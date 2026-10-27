import fcntl
import errno
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pytest

from research_project import catalog, catalog_io, runner
from research_project.catalog_io import (
    Settings,
    add_note,
    atomic_json,
    automatic_refresh,
    enqueue,
)
from research_project.example import generate, summarize


@pytest.fixture
def code(tmp_path: Path) -> Path:
    root = tmp_path / "code"
    (root / "pipeline/conf").mkdir(parents=True)
    (root / "pipeline/bin/research_project").mkdir(parents=True)
    (root / "results/runs").mkdir(parents=True)
    settings = Path(__file__).resolve().parents[2] / "conf/catalog.toml"
    shutil.copyfile(settings, root / "pipeline/conf/catalog.toml")
    (root / "pixi.toml").write_text("# fixture\n")
    (root / "pixi.lock").write_text("# fixture\n")
    return root


def record(
    code: Path,
    run_id: str = "run-001",
    *,
    state: str = "completed",
    split: str | None = "split-v1",
    parquet: bool = False,
) -> Path:
    result = code / "results/runs" / run_id
    (result / "example").mkdir(parents=True)
    generate(
        42,
        8,
        result / "example/samples.csv",
        result / "example/samples.parquet" if parquet else None,
        parquet_threads=1,
        parquet_memory_limit="128MB",
    )
    summarize(result / "example/samples.csv", result / "example/summary.json")
    outputs = ["example/samples.csv", "example/summary.json"]
    if parquet:
        outputs.append("example/samples.parquet")
    params = {
        "workflow_name": "example",
        "seed": 42,
        "sample_count": 8,
        "dataset_id": "synthetic-v1",
        "split_id": split,
        "biological_unit": "not-applicable-synthetic",
        "evaluation_id": "summary-v1",
        "expected_outputs": outputs,
        "catalog_metrics": [
            {
                "name": "mean",
                "path": "example/summary.json",
                "key": "mean",
                "unit": "dimensionless",
                "definition_id": "arithmetic-mean-v1",
            },
        ],
    }
    atomic_json(
        result / "manifest.json",
        {
            "run_id": run_id,
            "parameters": params,
            "source_digest": "a" * 64,
            "record_hashes": {"resolved-config.json": "b" * 64},
            "created_utc": "2026-09-26T00:00:00+00:00",
        },
    )
    atomic_json(
        result / "terminal.json",
        {"state": state, "exit_code": 0 if state == "completed" else 1},
    )
    atomic_json(
        result / "audit.json",
        {
            "status": "pass" if state == "completed" else "fail",
            "errors": [],
            "artifact_sha256": {name: runner.sha256(result / name) for name in outputs},
        },
    )
    return result


def child_refresh(code: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "research_project.catalog", "_refresh"],
        cwd=code,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen) -> dict:
    output, error = process.communicate(timeout=20)
    assert process.returncode == 0, error
    return json.loads(output)


def test_index_is_idempotent_and_keeps_provenance(code: Path) -> None:
    result = record(code)
    before = {
        str(path): path.read_bytes() for path in result.rglob("*") if path.is_file()
    }
    enqueue(code, "run-001")
    assert catalog.refresh(code)["status"] == "updated"
    enqueue(code, "run-001")
    enqueue(code, "run-001")
    catalog.refresh(code)
    assert catalog.pending(code) == 0
    evidence = catalog.get_evidence(code, "run-001")
    assert evidence["run"]["source_digest"] == "a" * 64
    assert evidence["run"]["errors_json"] == []
    assert len(evidence["metrics"]) == 1
    assert evidence["metrics"][0]["value"] == pytest.approx(0.4443708390149597)
    assert len(catalog.list_runs(code)["runs"]) == 1
    assert before == {
        str(path): path.read_bytes() for path in result.rglob("*") if path.is_file()
    }


def test_failed_incomplete_and_missing_cases_remain_visible(code: Path) -> None:
    failed = record(code, "failed", state="failed")
    (failed / "example/summary.json").unlink()
    partial = code / "results/runs/partial"
    partial.mkdir()
    atomic_json(partial / "terminal.json", {"state": "preparation_failed"})
    catalog.refresh(code, rebuild=True)
    assert {run["state"] for run in catalog.list_runs(code)["runs"]} == {
        "failed",
        "preparation_failed",
    }
    evidence = catalog.get_evidence(code, "failed")
    assert any(not artifact["present"] for artifact in evidence["artifacts"])
    assert evidence["metrics"][0]["value"] is None
    assert catalog.get_evidence(code, "partial")["run"]["audit_status"] == "missing"


def test_modified_metric_is_not_presented_as_audited(code: Path) -> None:
    result = record(code)
    atomic_json(result / "example/summary.json", {"mean": 0.99})
    catalog.refresh(code, rebuild=True)
    evidence = catalog.get_evidence(code, "run-001")
    assert evidence["run"]["audit_status"] == "pass"  # recorded verdict is preserved
    assert evidence["metrics"][0]["value"] is None
    assert "differs" in evidence["metrics"][0]["error"]
    assert evidence["run"]["errors_json"]


def test_malformed_manifest_records_gap_without_hiding_run(code: Path) -> None:
    result = record(code)
    (result / "manifest.json").write_text("{broken")
    catalog.refresh(code, rebuild=True)
    run = catalog.list_runs(code)["runs"][0]
    assert run["state"] == "completed"
    assert run["parameters_json"] == {}
    assert any("manifest.json" in error for error in run["errors_json"])


def test_comparison_flags_mismatched_and_unknown_splits(code: Path) -> None:
    record(code, "a", split="split-a")
    record(code, "b", split="split-b")
    record(code, "c", split=None)
    catalog.refresh(code, rebuild=True)
    assert "Mismatched split_id" in catalog.compare_runs(code, ["a", "b"])["issues"]
    assert (
        "Missing comparison provenance: split_id"
        in catalog.compare_runs(code, ["a", "c"])["issues"]
    )
    assert not catalog.compare_runs(code, ["a", "absent"])[
        "matching_recorded_definitions"
    ]


def test_metric_definition_mismatch_is_reported(code: Path) -> None:
    record(code, "a")
    second = record(code, "b")
    manifest = json.loads((second / "manifest.json").read_text())
    manifest["parameters"]["catalog_metrics"][0]["definition_id"] = "different-v2"
    atomic_json(second / "manifest.json", manifest)
    catalog.refresh(code, rebuild=True)
    assert (
        "Mismatched units or definition for metric: mean"
        in catalog.compare_runs(code, ["a", "b"])["issues"]
    )


def test_failed_publish_keeps_old_snapshot_and_requests(
    code: Path, monkeypatch
) -> None:
    record(code, "a")
    catalog.refresh(code, rebuild=True)
    old = catalog.pointer(code)
    record(code, "b")
    enqueue(code, "b")
    original = catalog.publish
    monkeypatch.setattr(
        catalog,
        "publish",
        lambda *args: (_ for _ in ()).throw(OSError("interrupted publication")),
    )
    with pytest.raises(OSError, match="interrupted"):
        catalog.refresh(code)
    assert catalog.pointer(code) == old
    assert catalog.pending(code) == 1
    assert len(catalog.list_runs(code)["runs"]) == 1
    monkeypatch.setattr(catalog, "publish", original)
    catalog.refresh(code)
    assert len(catalog.list_runs(code)["runs"]) == 2


def test_replay_after_publication_before_ack_is_safe(code: Path, monkeypatch) -> None:
    record(code)
    enqueue(code, "run-001")
    original = Path.unlink

    def fail_ack(path, *args, **kwargs):
        if path.parent.name == "queue" and path.suffix == ".json":
            raise OSError("simulated interruption before ack")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_ack)
    with pytest.raises(OSError, match="before ack"):
        catalog.refresh(code)
    assert len(catalog.list_runs(code)["runs"]) == 1
    assert catalog.pending(code) == 1
    monkeypatch.setattr(Path, "unlink", original)
    catalog.refresh(code)
    assert len(catalog.list_runs(code)["runs"]) == 1
    assert catalog.pending(code) == 0


def test_request_arriving_during_ingestion_is_not_acknowledged(
    code: Path, monkeypatch
) -> None:
    record(code)
    enqueue(code, "run-001")
    original = catalog.collect_run

    def concurrent_update(*args):
        collected = original(*args)
        add_note(
            code,
            "run-001",
            kind="decision",
            text="arrived during refresh",
            author="test",
        )
        enqueue(code, "run-001")
        return collected

    monkeypatch.setattr(catalog, "collect_run", concurrent_update)
    catalog.refresh(code)
    assert catalog.pending(code) == 1
    assert catalog.search_notes(code, "arrived")["notes"] == []
    monkeypatch.setattr(catalog, "collect_run", original)
    catalog.refresh(code)
    assert len(catalog.search_notes(code, "arrived")["notes"]) == 1
    assert catalog.pending(code) == 0


def test_multiple_processes_serialize_writes_without_losing_requests(
    code: Path,
) -> None:
    record(code, "a")
    record(code, "b")
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(
            pool.map(lambda i: enqueue(code, "a" if i % 2 else "b"), range(12))
        )
    assert len(set(identifiers)) == 12
    processes = [child_refresh(code), child_refresh(code)]
    replies = [finish(process) for process in processes]
    assert all(reply["status"] in {"updated", "unchanged", "busy"} for reply in replies)
    catalog.refresh(code)
    assert len(catalog.list_runs(code)["runs"]) == 2
    assert catalog.pending(code) == 0


def test_busy_writer_returns_without_waiting_for_lock(code: Path) -> None:
    record(code)
    enqueue(code, "run-001")
    root = catalog_io.catalog_directory(code)
    with (root / "writer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reply = finish(child_refresh(code))
    assert reply["status"] == "busy"
    assert catalog.pending(code) == 1
    assert catalog.refresh(code)["status"] == "updated"


def test_cleanup_supports_filesystems_requiring_writable_exclusive_locks(
    code: Path, monkeypatch
) -> None:
    original = fcntl.flock

    def filesystem_lock(handle, operation):
        descriptor = handle if isinstance(handle, int) else handle.fileno()
        if (
            operation & fcntl.LOCK_EX
            and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        ):
            raise OSError(errno.EBADF, "Exclusive locks require a writable descriptor")
        return original(handle, operation)

    monkeypatch.setattr(fcntl, "flock", filesystem_lock)
    record(code)
    assert catalog.refresh(code, rebuild=True)["status"] == "updated"
    assert len(catalog.list_runs(code)["runs"]) == 1


def test_readers_keep_snapshot_during_updates_and_cleanup(code: Path) -> None:
    record(code, "a")
    catalog.refresh(code, rebuild=True)
    with catalog.read_snapshot(code) as (reader, old, _):
        for i in range(4):
            record(code, f"b-{i}")
            enqueue(code, f"b-{i}")
            assert finish(child_refresh(code))["status"] == "updated"
        assert reader.execute("SELECT count(*) FROM runs").fetchone() == (1,)
        assert catalog.snapshot_path(code, old).is_file()
        assert len(catalog.list_runs(code)["runs"]) == 5
    catalog.refresh(code)
    assert (
        len(list((catalog_io.catalog_directory(code) / "snapshots").glob("*.duckdb")))
        <= Settings.load(code).snapshots_to_keep
    )


def test_notes_and_handoff_revisions_preserve_history(code: Path) -> None:
    record(code)
    first = add_note(
        code, "run-001", kind="decision", text="Keep 100% of units", author="researcher"
    )
    second = add_note(
        code,
        "run-001",
        kind="decision",
        text="Review the inclusion criteria",
        author="researcher",
        supersedes=first["note_id"],
    )
    runner.handoff(
        code, "run-001", objective="Original objective", next_step="Inspect evidence"
    )
    runner.handoff(
        code, "run-001", objective="Revised objective", next_step="Read the audit"
    )
    catalog.refresh(code, rebuild=True)
    assert catalog.search_notes(code, "100%")["notes"] == []
    assert len(catalog.search_notes(code, "100%", history=True)["notes"]) == 1
    assert (
        catalog.search_notes(code, "inclusion")["notes"][0]["note_id"]
        == second["note_id"]
    )
    assert catalog.search_notes(code, "Original")["notes"] == []
    assert len(catalog.search_notes(code, "objective", history=True)["notes"]) == 2


def test_existing_legacy_handoff_is_archived_before_update(code: Path) -> None:
    result = record(code)
    (result / "handoff.md").write_text("Legacy research objective")
    runner.handoff(
        code, "run-001", objective="Updated objective", next_step="Read evidence"
    )
    catalog.refresh(code, rebuild=True)
    assert len(catalog.search_notes(code, "Legacy", history=True)["notes"]) == 1
    assert catalog.search_notes(code, "Legacy")["notes"] == []


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM runs",
        "SELECT 1; SELECT 2",
        "COPY runs TO '/tmp/catalog-should-not-exist.csv'",
    ],
)
def test_sql_interface_accepts_one_read_query_only(code: Path, sql: str) -> None:
    record(code)
    catalog.refresh(code, rebuild=True)
    with pytest.raises(ValueError, match="SELECT"):
        catalog.select_query(code, sql)
    assert len(catalog.list_runs(code)["runs"]) == 1


def test_queries_are_bounded_and_do_not_read_source_records(
    code: Path, monkeypatch
) -> None:
    record(code)
    catalog.refresh(code, rebuild=True)
    monkeypatch.setattr(
        catalog,
        "read_document",
        lambda *args: (_ for _ in ()).throw(AssertionError("source scan")),
    )
    assert catalog.select_query(code, "SELECT run_id FROM runs")["rows"] == [
        {"run_id": "run-001"}
    ]
    assert catalog.select_query(code, "SELECT * FROM range(20)", limit=3)["truncated"]
    with pytest.raises(duckdb.Error):
        catalog.select_query(code, "SELECT * FROM read_text('/etc/passwd')")
    with pytest.raises(ValueError, match="limit"):
        catalog.list_runs(code, limit=0)


def test_expensive_query_is_interrupted(code: Path) -> None:
    record(code)
    catalog.refresh(code, rebuild=True)
    config = code / "pipeline/conf/catalog.toml"
    config.write_text(
        config.read_text().replace(
            "query_timeout_seconds = 10", "query_timeout_seconds = 0.05"
        )
    )
    with pytest.raises(duckdb.InterruptException):
        catalog.select_query(
            code, "SELECT sum(sin(i::DOUBLE)) FROM range(1000000000) r(i)"
        )


def test_parquet_query_uses_only_registered_artifacts(code: Path) -> None:
    record(code, parquet=True)
    catalog.refresh(code, rebuild=True)
    result = catalog.read_parquet(code, "run-001", "example/samples.parquet", limit=3)
    assert result["truncated"]
    assert [row["sample_id"] for row in result["rows"]] == [0, 1, 2]
    aggregate = catalog.read_parquet(
        code,
        "run-001",
        "example/samples.parquet",
        sql="SELECT count(*) AS n FROM result",
    )
    assert aggregate["rows"] == [{"n": 8}]
    with pytest.raises(duckdb.Error):
        catalog.read_parquet(
            code,
            "run-001",
            "example/samples.parquet",
            sql="SELECT * FROM read_text('/etc/passwd')",
        )
    with pytest.raises(ValueError):
        catalog.read_parquet(code, "run-001", "../../other.parquet")


def test_public_cli_refresh_note_search_and_query(code: Path) -> None:
    record(code)

    def cli(*arguments):
        process = subprocess.run(
            [sys.executable, "-m", "research_project.catalog", *arguments],
            cwd=code,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    assert cli("refresh", "--rebuild")["run_count"] == 1
    note = cli(
        "note",
        "--run-id",
        "run-001",
        "--author",
        "test",
        "--kind",
        "question",
        "--text",
        "Check specimen pairing",
    )
    assert note["indexing"]["status"] == "updated"
    assert (
        cli("search", "--query", "specimen")["notes"][0]["note_id"]
        == note["note"]["note_id"]
    )
    assert cli("--refresh", "runs")["refresh"]["status"] == "unchanged"
    assert cli("query", "--sql", "SELECT count(*) AS n FROM runs")["rows"] == [{"n": 1}]


def test_rebuild_recovers_without_queue_or_catalog(code: Path) -> None:
    record(code, "a")
    record(code, "b")
    assert catalog.pending(code) == 0
    catalog.refresh(code, rebuild=True)
    assert len(catalog.list_runs(code)["runs"]) == 2


def test_indexer_failure_does_not_fail_a_successful_experiment(
    code: Path, monkeypatch
) -> None:
    (code / "pipeline/conf/example.config").write_text("// fixture")
    params = {
        "workflow_name": "example",
        "seed": 42,
        "sample_count": 8,
        "driver_timeout_seconds": 60,
        "expected_outputs": ["example/summary.json"],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"params": params}), stderr=""
        ),
    )
    monkeypatch.setattr(runner, "execute", lambda *args, **kwargs: ("completed", 0))
    monkeypatch.setattr(runner, "audit", lambda *args: {"status": "pass", "errors": []})
    monkeypatch.setattr(
        catalog_io,
        "refresh_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("catalog unavailable")),
    )
    assert runner.run(code, "uninterrupted", "example.config") == 0
    assert runner.status(code, "uninterrupted")["state"] == "completed"
    assert catalog.pending(code) == 1


def test_timed_out_worker_retains_request(code: Path, monkeypatch) -> None:
    record(code)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(catalog_io.subprocess, "run", timeout)
    result = automatic_refresh(code, "run-001")
    assert result["status"] == "pending"
    assert catalog.pending(code) == 1
    assert runner.status(code, "run-001")["state"] == "completed"


def test_catalog_bounds_reject_unbounded_settings(code: Path) -> None:
    config = code / "pipeline/conf/catalog.toml"
    config.write_text(
        config.read_text().replace(
            "refresh_timeout_seconds = 30", "refresh_timeout_seconds = inf"
        )
    )
    with pytest.raises(ValueError, match="finite"):
        Settings.load(code)


def test_queries_over_one_thousand_recorded_runs(code: Path) -> None:
    # A reproducible scale fixture, not a wall-clock assertion sensitive to CI load.
    for number in range(1000):
        record(code, f"benchmark-{number:04d}")
    started = time.perf_counter()
    catalog.refresh(code, rebuild=True)
    index_seconds = time.perf_counter() - started
    listed = catalog.list_runs(code, limit=100)
    aggregate = catalog.select_query(
        code,
        "SELECT name, count(*) AS n, avg(value) AS value FROM metrics GROUP BY name",
    )
    assert listed["catalog"]["run_count"] == 1000
    assert len(listed["runs"]) == 100 and listed["truncated"]
    assert aggregate["rows"][0]["n"] == 1000
    print(
        "CATALOG_BENCHMARK "
        + json.dumps(
            {
                "runs": 1000,
                "metrics": 1000,
                "index_seconds": round(index_seconds, 3),
                "list_ms": listed["query_ms"],
                "aggregate_ms": aggregate["query_ms"],
                "scope": "Python API including snapshot open; generated fixtures on local temporary storage",
            }
        )
    )
