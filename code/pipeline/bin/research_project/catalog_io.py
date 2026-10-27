"""Durable catalog requests and research notes; no database import required."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_directory(code: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        raise ValueError("Invalid run ID")
    path = code / "results/runs" / run_id
    if path.is_symlink():
        raise ValueError("Run records must not be symlinks")
    return path


def contained(directory: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Expected a contained relative path: {relative}")
    resolved = (directory / path).resolve()
    resolved.relative_to(directory.resolve())
    return resolved


def atomic_text(path: Path, text: str) -> None:
    """Publish a complete file using a unique sibling and a same-directory rename."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=False)
        + "\n",
    )


@dataclass(frozen=True)
class Settings:
    auto_refresh: bool
    refresh_timeout_seconds: float
    query_timeout_seconds: float
    threads: int
    memory_limit: str
    max_requests_per_refresh: int
    max_runs_per_rebuild: int
    max_query_rows: int
    max_document_bytes: int
    max_notes_per_run: int
    snapshots_to_keep: int

    @classmethod
    def load(cls, code: Path) -> Settings:
        with (code / "pipeline/conf/catalog.toml").open("rb") as handle:
            settings = cls(**tomllib.load(handle))
        for name in ("refresh_timeout_seconds", "query_timeout_seconds"):
            value = getattr(settings, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"catalog {name} must be positive and finite")
        for name in (
            "threads",
            "max_requests_per_refresh",
            "max_runs_per_rebuild",
            "max_query_rows",
            "max_document_bytes",
            "max_notes_per_run",
            "snapshots_to_keep",
        ):
            value = getattr(settings, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"catalog {name} must be a positive integer")
        if type(settings.auto_refresh) is not bool:
            raise ValueError("catalog auto_refresh must be a boolean")
        if not isinstance(settings.memory_limit, str) or not re.fullmatch(
            r"[1-9][0-9]*(KB|MB|GB)", settings.memory_limit
        ):
            raise ValueError("catalog memory_limit must be a finite size, e.g. 512MB")
        return settings


def catalog_directory(code: Path) -> Path:
    return code / "data/store/catalog"


def enqueue(code: Path, run_id: str) -> str:
    if not run_directory(code, run_id).is_dir():
        raise ValueError(f"No run record exists for {run_id}")
    queue = catalog_directory(code) / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    atomic_json(
        queue / f"{request_id}.json",
        {
            "request_id": request_id,
            "run_id": run_id,
            "created_utc": utc_now(),
        },
    )
    return request_id


def refresh_process(code: Path, *, rebuild: bool = False) -> dict[str, Any]:
    settings = Settings.load(code)
    command = [sys.executable, "-m", "research_project.catalog", "_refresh"]
    if rebuild:
        command.append("--rebuild")
    environment = {
        **os.environ,
        "PYTHONPATH": str(code / "pipeline/bin"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        # Parent cleanup also runs when the bounded worker is killed on timeout.
        with tempfile.TemporaryDirectory(
            prefix="research-catalog-worker-"
        ) as temporary:
            environment["RESEARCH_CATALOG_TMP"] = temporary
            process = subprocess.run(
                command,
                cwd=code,
                env=environment,
                capture_output=True,
                text=True,
                timeout=settings.refresh_timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        return {"status": "pending", "reason": "Indexer timed out; requests retained"}
    if process.returncode:
        return {
            "status": "pending",
            "reason": process.stderr.strip()[-4000:] or "Indexer failed",
        }
    return json.loads(process.stdout)


def automatic_refresh(code: Path, run_id: str) -> dict[str, Any]:
    """A catalog failure must never change an experiment's exit status."""
    try:
        request_id = enqueue(code, run_id)
        settings = Settings.load(code)
        result = (
            refresh_process(code)
            if settings.auto_refresh
            else {"status": "pending", "reason": "Automatic refresh disabled"}
        )
        result = {**result, "request_id": request_id, "attempted_utc": utc_now()}
        atomic_json(run_directory(code, run_id) / "catalog-index.json", result)
    except Exception as error:
        # Deliberately broad at this optional integration boundary. Evidence is
        # already saved; a rebuild can recover even a failed enqueue operation.
        result = {"status": "pending", "reason": str(error)}
    if result["status"] not in {"updated", "unchanged"}:
        print(
            f"Catalog pending for {run_id}: {result.get('reason', result['status'])}",
            file=sys.stderr,
        )
    return result


def add_note(
    code: Path,
    run_id: str,
    *,
    kind: str,
    text: str,
    author: str,
    supersedes: str | None = None,
) -> dict[str, Any]:
    directory = run_directory(code, run_id)
    if not directory.is_dir():
        raise ValueError(f"No run record exists for {run_id}")
    if kind not in {"observation", "decision", "question", "next_action", "handoff"}:
        raise ValueError("Unknown note kind")
    if not text.strip() or not author.strip():
        raise ValueError("Notes require nonempty text and an author")
    if supersedes is not None:
        if (
            not re.fullmatch(r"[a-f0-9]{32}", supersedes)
            or not (directory / "notes" / f"{supersedes}.json").is_file()
        ):
            raise ValueError("The superseded note must exist in the same run")
    note = {
        "note_id": uuid.uuid4().hex,
        "run_id": run_id,
        "kind": kind,
        "text": text,
        "author": author,
        "created_utc": utc_now(),
        "supersedes": supersedes,
    }
    notes = directory / "notes"
    notes.mkdir(exist_ok=True)
    atomic_json(notes / f"{note['note_id']}.json", note)
    return note
