"""Rebuildable DuckDB snapshots over research records and Parquet artifacts."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import duckdb

from .catalog_io import (
    Settings,
    add_note,
    atomic_json,
    automatic_refresh,
    catalog_directory,
    contained,
    refresh_process,
    run_directory,
    utc_now,
)
from .runner import locate_code_root, state_root

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE catalog_meta (schema_version INTEGER);
INSERT INTO catalog_meta VALUES (1);
CREATE TABLE runs (
    run_id VARCHAR PRIMARY KEY, workflow VARCHAR, state VARCHAR, audit_status VARCHAR,
    created_utc VARCHAR, finished_utc VARCHAR, seed VARCHAR,
    dataset_id VARCHAR, split_id VARCHAR, biological_unit VARCHAR, evaluation_id VARCHAR,
    source_digest VARCHAR, config_digest VARCHAR, parameters_json JSON,
    manifest_path VARCHAR, record_digest VARCHAR, indexed_at VARCHAR, errors_json JSON
);
CREATE TABLE artifacts (
    run_id VARCHAR, path VARCHAR, format VARCHAR, expected BOOLEAN, present BOOLEAN,
    recorded_sha256 VARCHAR, size_bytes BIGINT, PRIMARY KEY (run_id, path)
);
CREATE TABLE metrics (
    run_id VARCHAR, name VARCHAR, value DOUBLE, unit VARCHAR, definition_id VARCHAR,
    source_path VARCHAR, source_sha256 VARCHAR, error VARCHAR, PRIMARY KEY (run_id, name)
);
CREATE TABLE notes (
    note_id VARCHAR PRIMARY KEY, run_id VARCHAR, kind VARCHAR, text VARCHAR,
    author VARCHAR, created_utc VARCHAR, source_path VARCHAR, source_sha256 VARCHAR,
    supersedes VARCHAR, is_current BOOLEAN
);
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pending(code: Path) -> int:
    return sum(1 for _ in (catalog_directory(code) / "queue").glob("*.json"))


def pointer(code: Path) -> dict[str, Any] | None:
    path = catalog_directory(code) / "current.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Catalog schema changed; run catalog refresh --rebuild")
    if not re.fullmatch(r"[a-f0-9]{32}", record.get("snapshot_id", "")):
        raise ValueError("Invalid catalog snapshot ID")
    return record


def snapshot_path(code: Path, record: dict[str, Any]) -> Path:
    return catalog_directory(code) / "snapshots" / f"{record['snapshot_id']}.duckdb"


def connection_options(settings: Settings) -> dict[str, Any]:
    return {
        "threads": settings.threads,
        "memory_limit": settings.memory_limit,
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }


def read_document(path: Path, settings: Settings) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(settings.max_document_bytes + 1)
    if len(data) > settings.max_document_bytes:
        raise ValueError(f"Document exceeds catalog byte limit: {path.name}")
    return data


def collect_run(code: Path, run_id: str, settings: Settings) -> dict[str, Any]:
    """Index recorded evidence. Never infer missing provenance or re-run an audit."""
    directory = run_directory(code, run_id)
    errors: list[str] = []
    observed: dict[str, str] = {}

    def document(relative: str, *, required: bool = True) -> dict[str, Any]:
        try:
            path = contained(directory, relative)
            data = read_document(path, settings)
            observed[relative] = digest(data)
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except (OSError, ValueError) as error:
            if required or not isinstance(error, FileNotFoundError):
                errors.append(f"{relative}: {error}")
            return {}

    manifest = document("manifest.json")
    terminal = document("terminal.json")
    audit = document("audit.json")
    if manifest.get("run_id", run_id) != run_id:
        errors.append("Manifest run ID differs from its directory")
    params = manifest.get("parameters", {})
    if not isinstance(params, dict):
        errors.append("Manifest parameters must be an object")
        params = {}
    hashes = audit.get("artifact_sha256", {})
    if not isinstance(hashes, dict):
        hashes = {}
        errors.append("Audit artifact hashes must be an object")
    expected = params.get("expected_outputs", [])
    if not isinstance(expected, list) or not all(
        isinstance(name, str) for name in expected
    ):
        errors.append("expected_outputs must be a list of paths")
        expected = []
    artifacts = []
    for relative in dict.fromkeys(expected):
        try:
            path = contained(directory, relative)
            exists = path.is_file()
            artifacts.append(
                (
                    run_id,
                    relative,
                    path.suffix.lstrip("."),
                    True,
                    exists,
                    hashes.get(relative),
                    path.stat().st_size if exists else None,
                )
            )
            if not exists:
                errors.append(f"Missing expected artifact: {relative}")
        except (OSError, ValueError) as error:
            errors.append(f"Artifact {relative}: {error}")
            artifacts.append((run_id, relative, "unknown", True, False, None, None))

    specs = params.get("catalog_metrics", [])
    if not isinstance(specs, list):
        errors.append("catalog_metrics must be a list")
        specs = []
    metrics, names = [], set()
    for spec in specs:
        try:
            if not isinstance(spec, dict) or not all(
                isinstance(spec.get(key), str) and spec[key]
                for key in ("name", "path", "key", "unit", "definition_id")
            ):
                raise ValueError(
                    "Metric requires name, path, key, unit and definition_id"
                )
            if spec["name"] in names:
                raise ValueError(f"Duplicate metric name: {spec['name']}")
            names.add(spec["name"])
            if spec["path"] not in expected:
                raise ValueError("Metric source must be a declared expected output")
            value = document(spec["path"]).get(spec["key"])
            error = None
            if type(value) not in (int, float) or not math.isfinite(value):
                value, error = None, "Missing or nonfinite metric value"
            actual_hash = observed.get(spec["path"])
            if hashes.get(spec["path"]) and actual_hash != hashes[spec["path"]]:
                value, error = (
                    None,
                    "Metric source differs from the recorded audit hash",
                )
            if error:
                errors.append(f"Metric {spec['name']}: {error}")
            metrics.append(
                (
                    run_id,
                    spec["name"],
                    value,
                    spec["unit"],
                    spec["definition_id"],
                    spec["path"],
                    actual_hash,
                    error,
                )
            )
        except (TypeError, ValueError) as error:
            errors.append(f"Metric definition: {error}")

    notes = []
    paths = sorted((directory / "notes").glob("*.json"))
    if len(paths) > settings.max_notes_per_run:
        raise ValueError(f"{run_id} exceeds max_notes_per_run")
    for path in paths:
        relative = str(path.relative_to(directory))
        note = document(relative)
        if not note:
            continue
        if (
            not re.fullmatch(r"[a-f0-9]{32}", str(note.get("note_id", "")))
            or path.stem != note["note_id"]
            or note.get("run_id") != run_id
            or not all(
                isinstance(note.get(key), str)
                for key in ("kind", "text", "author", "created_utc")
            )
        ):
            errors.append(f"Invalid note identity or fields: {relative}")
            continue
        notes.append(
            (
                note["note_id"],
                run_id,
                note["kind"],
                note["text"],
                note["author"],
                note["created_utc"],
                relative,
                observed[relative],
                note.get("supersedes"),
                True,
            )
        )
    superseded = {note[8] for note in notes if note[8]}
    notes = [(*note[:-1], note[0] not in superseded) for note in notes]
    if (
        not any(note[2] == "handoff" for note in notes)
        and (directory / "handoff.md").is_file()
    ):
        data = read_document(contained(directory, "handoff.md"), settings)
        observed["handoff.md"] = digest(data)
        notes.append(
            (
                f"legacy-handoff:{run_id}",
                run_id,
                "handoff",
                data.decode(),
                "unrecorded",
                None,
                "handoff.md",
                digest(data),
                None,
                True,
            )
        )

    # Do not acknowledge a request when a mutable input changed during collection.
    for relative, original in observed.items():
        if digest(read_document(contained(directory, relative), settings)) != original:
            raise ValueError(f"Run record changed during indexing: {run_id}/{relative}")
    source_digest = manifest.get("source_digest")
    record_hashes = manifest.get("record_hashes", {})
    config_digest = (
        record_hashes.get("resolved-config.json")
        if isinstance(record_hashes, dict)
        else None
    )
    if not source_digest or not config_digest:
        errors.append("Missing source or resolved-configuration digest")
    audit_status = audit.get("status", "missing")
    if audit_status == "pass" and errors:
        # Preserve the recorded verdict, while keeping new catalog observations separate.
        errors.append(
            "Recorded audit passed; catalog observations contain additional gaps"
        )
    indexed_at = utc_now()
    audit_errors = audit.get("errors", [])
    if not isinstance(audit_errors, list) or not all(
        isinstance(error, str) for error in audit_errors
    ):
        audit_errors = ["Malformed audit errors"]
    row = (
        run_id,
        params.get("workflow_name"),
        terminal.get("state", "unknown"),
        audit_status,
        manifest.get("created_utc", terminal.get("started_utc")),
        terminal.get("finished_utc"),
        str(params["seed"]) if "seed" in params else None,
        *(
            params.get(key)
            for key in ("dataset_id", "split_id", "biological_unit", "evaluation_id")
        ),
        source_digest,
        config_digest,
        json.dumps(params, allow_nan=False),
        f"results/runs/{run_id}/manifest.json",
        digest(json.dumps(observed, sort_keys=True).encode()),
        indexed_at,
        json.dumps(errors + audit_errors),
    )
    return {"run": row, "artifacts": artifacts, "metrics": metrics, "notes": notes}


def insert_record(
    connection: duckdb.DuckDBPyConnection, record: dict[str, Any]
) -> None:
    run_id = record["run"][0]
    connection.execute("BEGIN")
    try:
        for table in ("metrics", "artifacts", "notes", "runs"):
            connection.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
        connection.execute(
            "INSERT INTO runs VALUES (" + ",".join("?" for _ in record["run"]) + ")",
            record["run"],
        )
        for table in ("artifacts", "metrics", "notes"):
            rows = record[table]
            if rows:
                connection.executemany(
                    f"INSERT INTO {table} VALUES ("
                    + ",".join("?" for _ in rows[0])
                    + ")",
                    rows,
                )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def publish(code: Path, database: Path, record: dict[str, Any]) -> None:
    destination = snapshot_path(code, record)
    staging = destination.with_suffix(".tmp")
    with database.open("rb") as source, staging.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    staging.replace(destination)
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # The DB has closed and checkpointed before its immutable snapshot is exposed.
    atomic_json(catalog_directory(code) / "current.json", record)


def prune_snapshots(code: Path, settings: Settings, current_id: str) -> None:
    # Queries hold a shared guard from pointer lookup through connection close.
    # If any are active, defer cleanup; publishing itself never waits for readers.
    # NFS implements flock through byte-range locks and requires a writable
    # descriptor for an exclusive lock, even though this file's contents are unused.
    with (catalog_directory(code) / "readers.lock").open("r+") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        paths = sorted(
            (catalog_directory(code) / "snapshots").glob("*.duckdb"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        keep = {path.stem for path in paths[: settings.snapshots_to_keep]} | {
            current_id
        }
        for path in paths:
            if path.stem not in keep:
                path.unlink()
        # A killed build may leave an unpublished copy; the single writer owns these.
        for path in (catalog_directory(code) / "snapshots").glob("*.tmp"):
            path.unlink()


def refresh(code: Path, *, rebuild: bool = False) -> dict[str, Any]:
    """One bounded ingestion batch. Caller runs this in a timed subprocess."""
    settings = Settings.load(code)
    root = catalog_directory(code)
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    (root / "readers.lock").touch(exist_ok=True)
    with (root / "writer.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "busy",
                "reason": "Another indexer owns the writer lock",
                "pending_requests": pending(code),
            }
        old = None if rebuild else pointer(code)
        requests: dict[str, list[Path]] = {}
        failures: dict[str, str] = {}
        for path in sorted((root / "queue").glob("*.json"))[
            : settings.max_requests_per_refresh
        ]:
            try:
                request = json.loads(read_document(path, settings))
                if request["request_id"] != path.stem:
                    raise ValueError("Request ID differs from filename")
                run_directory(code, request["run_id"])
                requests.setdefault(request["run_id"], []).append(path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                failures[path.name] = str(error)
        run_ids = set(requests)
        if rebuild:
            for path in (code / "results/runs").iterdir():
                if path.is_dir():
                    run_directory(code, path.name)
                    run_ids.add(path.name)
                if len(run_ids) > settings.max_runs_per_rebuild:
                    raise ValueError(
                        "Rebuild exceeds max_runs_per_rebuild; increase the configured bound"
                    )
        if not run_ids and old:
            prune_snapshots(code, settings, old["snapshot_id"])
            return {
                "status": "pending" if failures else "unchanged",
                "snapshot_id": old["snapshot_id"],
                "failures": failures,
                "pending_requests": pending(code),
            }
        succeeded = []
        with tempfile.TemporaryDirectory(
            prefix="research-catalog-", dir=os.environ.get("RESEARCH_CATALOG_TMP")
        ) as temporary:
            database = Path(temporary) / "catalog.duckdb"
            if old:
                shutil.copyfile(snapshot_path(code, old), database)
            with duckdb.connect(
                str(database), config=connection_options(settings)
            ) as connection:
                if not old:
                    connection.execute(SCHEMA)
                elif connection.execute(
                    "SELECT schema_version FROM catalog_meta"
                ).fetchone() != (SCHEMA_VERSION,):
                    raise ValueError(
                        "Catalog schema changed; run catalog refresh --rebuild"
                    )
                for run_id in sorted(run_ids):
                    try:
                        insert_record(connection, collect_run(code, run_id, settings))
                        succeeded.append(run_id)
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        duckdb.Error,
                    ) as error:
                        failures[run_id] = str(error)
                if rebuild and failures:
                    # Keep the previous complete generation when a rebuild is incomplete.
                    return {
                        "status": "pending",
                        "failures": failures,
                        "pending_requests": pending(code),
                    }
                count = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
                connection.execute("CHECKPOINT")
            record = {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": uuid.uuid4().hex,
                "indexed_at": utc_now(),
                "run_count": count,
                "failures": failures,
                "settings_sha256": digest(
                    (code / "pipeline/conf/catalog.toml").read_bytes()
                ),
            }
            publish(code, database, record)
        # Ack only the exact requests collected before publication. New requests,
        # failures and interrupted updates remain discoverable for the next refresh.
        for run_id in succeeded:
            for path in requests.get(run_id, []):
                path.unlink(missing_ok=True)
        prune_snapshots(code, settings, record["snapshot_id"])
        return {
            "status": "pending" if failures else "updated",
            **record,
            "indexed_runs": succeeded,
            "pending_requests": pending(code),
        }


@contextlib.contextmanager
def read_snapshot(
    code: Path,
) -> Iterator[tuple[duckdb.DuckDBPyConnection, dict[str, Any], Settings]]:
    settings = Settings.load(code)
    root = catalog_directory(code)
    if not (root / "readers.lock").exists():
        raise ValueError("No catalog yet; run catalog refresh --rebuild")
    with (root / "readers.lock").open("r") as guard:
        fcntl.flock(guard, fcntl.LOCK_SH)
        record = pointer(code)
        if record is None:
            raise ValueError("No catalog yet; run catalog refresh --rebuild")
        with duckdb.connect(
            str(snapshot_path(code, record)),
            read_only=True,
            config=connection_options(settings),
        ) as connection:
            timer = threading.Timer(
                settings.query_timeout_seconds, connection.interrupt
            )
            timer.daemon = True
            timer.start()
            try:
                yield (
                    connection,
                    {**record, "pending_requests": pending(code)},
                    settings,
                )
            finally:
                timer.cancel()
                timer.join()


def rows(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    values: list[Any] | None = None,
    *,
    limit: int = 1000,
) -> tuple[list[dict[str, Any]], bool]:
    result = connection.execute(sql, values or [])
    columns = [column[0] for column in result.description]
    records = result.fetchmany(limit + 1)
    output = []
    for record in records[:limit]:
        item = dict(zip(columns, record))
        for key in item:
            if key.endswith("_json") and isinstance(item[key], str):
                item[key] = json.loads(item[key])
        output.append(item)
    return output, len(records) > limit


def select_query(code: Path, sql: str, *, limit: int = 100) -> dict[str, Any]:
    started = time.perf_counter()
    with read_snapshot(code) as (connection, metadata, settings):
        if not 1 <= limit <= settings.max_query_rows:
            raise ValueError(f"limit must be between 1 and {settings.max_query_rows}")
        validate_select(connection, sql)
        records, truncated = rows(connection, sql, limit=limit)
    return {
        "catalog": metadata,
        "rows": records,
        "truncated": truncated,
        "query_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def validate_select(connection: duckdb.DuckDBPyConnection, sql: str) -> None:
    statements = connection.extract_statements(sql)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Only one SELECT query is allowed")


def list_runs(
    code: Path,
    *,
    state: str | None = None,
    dataset_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    started = time.perf_counter()
    with read_snapshot(code) as (connection, metadata, settings):
        if not 1 <= limit <= settings.max_query_rows:
            raise ValueError(f"limit must be between 1 and {settings.max_query_rows}")
        records, truncated = rows(
            connection,
            "SELECT * FROM runs WHERE (? IS NULL OR state = ?) AND (? IS NULL OR dataset_id = ?) "
            "ORDER BY created_utc DESC NULLS LAST, run_id",
            [state, state, dataset_id, dataset_id],
            limit=limit,
        )
    return {
        "catalog": metadata,
        "runs": records,
        "truncated": truncated,
        "query_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def get_evidence(code: Path, run_id: str) -> dict[str, Any]:
    directory = run_directory(code, run_id)
    with read_snapshot(code) as (connection, metadata, settings):
        found, _ = rows(connection, "SELECT * FROM runs WHERE run_id = ?", [run_id])
        if not found:
            raise ValueError(f"Run is not indexed: {run_id}")
        result = {"catalog": metadata, "run": found[0], "truncated": {}}
        for table in ("artifacts", "metrics", "notes"):
            result[table], result["truncated"][table] = rows(
                connection,
                f"SELECT * FROM {table} WHERE run_id = ? ORDER BY 2",
                [run_id],
                limit=settings.max_query_rows,
            )
        result["record_directory"] = str(directory.resolve())
        result["verification"] = (
            "Recorded audit and hashes; use the audit command for fresh verification"
        )
    return result


def compare_runs(code: Path, run_ids: list[str]) -> dict[str, Any]:
    if not 2 <= len(set(run_ids)) == len(run_ids) <= 20:
        raise ValueError("Compare between 2 and 20 distinct run IDs")
    for run_id in run_ids:
        run_directory(code, run_id)
    with read_snapshot(code) as (connection, metadata, settings):
        placeholders = ",".join("?" for _ in run_ids)
        runs, _ = rows(
            connection,
            f"SELECT * FROM runs WHERE run_id IN ({placeholders}) ORDER BY run_id",
            run_ids,
        )
        metrics, truncated = rows(
            connection,
            f"SELECT * FROM metrics WHERE run_id IN ({placeholders}) ORDER BY name, run_id",
            run_ids,
            limit=settings.max_query_rows,
        )
    issues = []
    missing = sorted(set(run_ids) - {run["run_id"] for run in runs})
    if missing:
        issues.append(f"Runs not indexed: {', '.join(missing)}")
    for field in (
        "workflow",
        "dataset_id",
        "split_id",
        "biological_unit",
        "evaluation_id",
    ):
        values = {run[field] for run in runs}
        if None in values or "" in values:
            issues.append(f"Missing comparison provenance: {field}")
        if len(values) > 1:
            issues.append(f"Mismatched {field}")
    for run in runs:
        if (
            run["state"] != "completed"
            or run["audit_status"] != "pass"
            or run["errors_json"]
        ):
            issues.append(
                f"{run['run_id']}: unsuccessful run, failed/missing audit, or provenance gaps"
            )
    for name in sorted({metric["name"] for metric in metrics}):
        group = [metric for metric in metrics if metric["name"] == name]
        if len(group) != len(run_ids) or any(
            metric["value"] is None for metric in group
        ):
            issues.append(f"Missing values for metric: {name}")
        if len({(metric["unit"], metric["definition_id"]) for metric in group}) != 1:
            issues.append(f"Mismatched units or definition for metric: {name}")
    if not metrics or truncated:
        issues.append("No metrics available or metric result exceeds row limit")
    return {
        "catalog": metadata,
        "runs": runs,
        "metrics": metrics,
        "issues": issues,
        "matching_recorded_definitions": not issues,
        "interpretation": "Side-by-side recorded values; no model ranking or biological-validity claim",
    }


def search_notes(
    code: Path, query: str, *, limit: int = 20, history: bool = False
) -> dict[str, Any]:
    terms = query.lower().split()
    if not terms or len(terms) > 20:
        raise ValueError("Search requires 1–20 terms")
    with read_snapshot(code) as (connection, metadata, settings):
        if not 1 <= limit <= settings.max_query_rows:
            raise ValueError(f"limit must be between 1 and {settings.max_query_rows}")
        predicates = " AND ".join("contains(lower(text), ?)" for _ in terms)
        records, truncated = rows(
            connection,
            f"SELECT * FROM notes WHERE ({predicates}) AND (? OR is_current) "
            "ORDER BY created_utc DESC NULLS LAST, note_id",
            [*terms, history],
            limit=limit,
        )
    return {
        "catalog": metadata,
        "notes": records,
        "truncated": truncated,
        "search_type": "all-terms keyword",
    }


def read_parquet(
    code: Path,
    run_id: str,
    relative: str,
    *,
    limit: int = 100,
    sql: str = "SELECT * FROM result",
) -> dict[str, Any]:
    with read_snapshot(code) as (connection, metadata, settings):
        if not 1 <= limit <= settings.max_query_rows:
            raise ValueError(f"limit must be between 1 and {settings.max_query_rows}")
        artifacts, _ = rows(
            connection,
            "SELECT * FROM artifacts WHERE run_id = ? AND path = ? AND format = 'parquet' AND present",
            [run_id, relative],
        )
    if not artifacts:
        raise ValueError("Parquet artifact is not registered as present in the catalog")
    path = contained(run_directory(code, run_id), relative)
    options = connection_options(settings)
    # DuckDB requires path permissions to be set after connection startup and
    # before external access is disabled. No user SQL runs during this setup.
    options["enable_external_access"] = True
    with duckdb.connect(config=options) as connection:
        literal = "'" + str(path).replace("'", "''") + "'"
        connection.execute(f"SET allowed_paths = [{literal}]")
        connection.execute("SET enable_external_access = false")
        timer = threading.Timer(settings.query_timeout_seconds, connection.interrupt)
        timer.daemon = True
        timer.start()
        try:
            validate_select(connection, sql)
            connection.read_parquet(str(path)).create_view("result")
            records, truncated = rows(connection, sql, limit=limit)
        finally:
            timer.cancel()
            timer.join()
    return {
        "catalog": metadata,
        "artifact": artifacts[0],
        "rows": records,
        "truncated": truncated,
        "verification": "Reads the current artifact; recorded hash is not rechecked. Use audit for verification.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh_before_query",
        help="Attempt a bounded refresh before querying; otherwise use the latest snapshot",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("refresh", "_refresh"):
        command = commands.add_parser(
            name, help="Reconcile pending requests; --rebuild scans all run records"
        )
        command.add_argument("--rebuild", action="store_true")
    commands.add_parser("status")
    runs = commands.add_parser("runs")
    runs.add_argument("--state")
    runs.add_argument("--dataset-id")
    runs.add_argument("--limit", type=int, default=100)
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--run-id", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--run-id", action="append", required=True, dest="run_ids")
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--history", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    query = commands.add_parser("query")
    query.add_argument("--sql", required=True)
    query.add_argument("--limit", type=int, default=100)
    parquet = commands.add_parser("parquet")
    parquet.add_argument("--run-id", required=True)
    parquet.add_argument("--path", required=True)
    parquet.add_argument("--limit", type=int, default=100)
    parquet.add_argument(
        "--sql",
        default="SELECT * FROM result",
        help="SELECT over the registered Parquet view named result",
    )
    note = commands.add_parser("note")
    note.add_argument("--run-id", required=True)
    note.add_argument(
        "--kind",
        choices=["observation", "decision", "question", "next_action"],
        default="observation",
    )
    note.add_argument("--text", required=True)
    note.add_argument("--author", required=True)
    note.add_argument("--supersedes")
    args = parser.parse_args()
    try:
        # Worktrees share the main checkout's catalog.
        code = state_root(locate_code_root())
        maintenance = None
        if args.refresh_before_query and args.action in {
            "status",
            "runs",
            "evidence",
            "compare",
            "search",
            "query",
            "parquet",
        }:
            maintenance = refresh_process(code)
        if args.action in {"refresh", "_refresh"}:
            result = (
                refresh(code, rebuild=args.rebuild)
                if args.action == "_refresh"
                else refresh_process(code, rebuild=args.rebuild)
            )
        elif args.action == "status":
            result = {"catalog": pointer(code), "pending_requests": pending(code)}
        elif args.action == "runs":
            result = list_runs(
                code, state=args.state, dataset_id=args.dataset_id, limit=args.limit
            )
        elif args.action == "evidence":
            result = get_evidence(code, args.run_id)
        elif args.action == "compare":
            result = compare_runs(code, args.run_ids)
        elif args.action == "search":
            result = search_notes(
                code, args.query, limit=args.limit, history=args.history
            )
        elif args.action == "query":
            result = select_query(code, args.sql, limit=args.limit)
        elif args.action == "parquet":
            result = read_parquet(
                code, args.run_id, args.path, limit=args.limit, sql=args.sql
            )
        else:
            settings = Settings.load(code)
            if len(args.text.encode()) > settings.max_document_bytes // 2:
                raise ValueError("Note exceeds catalog document limit")
            saved = add_note(
                code,
                args.run_id,
                kind=args.kind,
                text=args.text,
                author=args.author,
                supersedes=args.supersedes,
            )
            result = {"note": saved, "indexing": automatic_refresh(code, args.run_id)}
        if maintenance is not None:
            result["refresh"] = maintenance
        print(json.dumps(result, indent=2, allow_nan=False, default=str))
        if args.action == "refresh" and result["status"] in {"pending", "busy"}:
            raise SystemExit(1)
    except (OSError, ValueError, TypeError, KeyError, duckdb.Error) as error:
        parser.exit(1, f"Catalog error: {error}\n")


if __name__ == "__main__":
    main()
