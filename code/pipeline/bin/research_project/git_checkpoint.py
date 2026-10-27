"""Create concurrency-safe Git branches for recorded experiments."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


CHECKPOINT_NAME = "git-checkpoint.json"
TERMINAL_STATES = {
    "completed",
    "failed",
    "timed_out",
    "interrupted",
    "audit_failed",
    "preparation_failed",
}


class GitCheckpointError(ValueError):
    """A required experiment checkpoint could not be established."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise GitCheckpointError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _git(
    project: Path,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        capture_output=True,
        text=True,
        input=input_text,
        env=environment,
        timeout=20,
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitCheckpointError(detail or f"Git command failed: {' '.join(arguments)}")
    return completed


def require_git_repository(project: Path) -> str:
    """Return HEAD after checking that commits can be created."""
    if shutil.which("git") is None:
        raise GitCheckpointError("Git is unavailable")
    inside = _git(project, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode or inside.stdout.strip() != "true":
        raise GitCheckpointError(
            "Initialize this project as a Git repository before launching experiments"
        )
    revision = _git(project, ["rev-parse", "--verify", "HEAD"], check=False)
    if revision.returncode:
        raise GitCheckpointError(
            "Create the initial project commit before launching experiments"
        )
    identity = _git(project, ["var", "GIT_AUTHOR_IDENT"], check=False)
    if identity.returncode:
        raise GitCheckpointError(
            "Configure user.name and user.email before launching experiments"
        )
    return revision.stdout.strip()


def _run_kind(run_id: str, manifest: dict[str, Any]) -> str:
    kind = manifest.get("run_kind")
    if isinstance(kind, str):
        return kind
    return "validation" if run_id.startswith("smoke-") else "experiment"


def latest_experiment(code: Path) -> str | None:
    """Return the newest non-validation run, including incomplete records."""
    records = code / "results/runs"
    candidates: list[tuple[str, str]] = []
    if not records.is_dir():
        return None
    for directory in records.iterdir():
        if not directory.is_dir():
            continue
        manifest_path = directory / "manifest.json"
        terminal_path = directory / "terminal.json"
        manifest: dict[str, Any] = {}
        terminal: dict[str, Any] = {}
        try:
            if manifest_path.is_file():
                manifest = _read_json(manifest_path)
            if terminal_path.is_file():
                terminal = _read_json(terminal_path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if _run_kind(directory.name, manifest) != "experiment":
            continue
        timestamp = str(
            manifest.get("created_utc")
            or terminal.get("started_utc")
            or directory.stat().st_mtime_ns
        )
        candidates.append((timestamp, directory.name))
    return max(candidates)[1] if candidates else None


def _valid_existing(
    project: Path,
    run_id: str,
    source_digest: str,
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    expected_branch = f"experiment/{run_id}"
    if (
        checkpoint.get("status") != "created"
        or checkpoint.get("branch") != expected_branch
        or checkpoint.get("source_digest") != source_digest
        or not isinstance(checkpoint.get("commit"), str)
    ):
        return None
    reference = _git(
        project,
        ["rev-parse", "--verify", f"refs/heads/{expected_branch}"],
        check=False,
    )
    if reference.returncode or reference.stdout.strip() != checkpoint["commit"]:
        return None
    return {**checkpoint, "status": "existing"}


def checkpoint_experiment(code: Path, run_id: str) -> dict[str, Any]:
    """Commit a recorded run's frozen source on experiment/<run_id>."""
    from .runner import inventory_digest, run_paths, source_inventory

    launch, result, _ = run_paths(code, run_id)
    checkpoint_path = result / CHECKPOINT_NAME
    branch = f"experiment/{run_id}"
    try:
        project = code.resolve().parent
        current_head = require_git_repository(project)
        manifest = _read_json(result / "manifest.json")
        terminal = _read_json(result / "terminal.json")
        if terminal.get("state") not in TERMINAL_STATES:
            raise GitCheckpointError(
                f"Run {run_id} is not terminal: {terminal.get('state', 'unknown')}"
            )
        if manifest.get("run_id") != run_id:
            raise GitCheckpointError(
                "Run manifest identity does not match its directory"
            )
        if _run_kind(run_id, manifest) != "experiment":
            raise GitCheckpointError(
                "Validation runs do not receive experiment branches"
            )

        base = manifest.get("git", {}).get("revision")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", base):
            raise GitCheckpointError(
                "The experiment did not record a usable Git revision at launch"
            )
        if _git(
            project, ["cat-file", "-e", f"{base}^{{commit}}"], check=False
        ).returncode:
            raise GitCheckpointError("The recorded launch revision is unavailable")

        frozen = launch / "source/code"
        inventory = source_inventory(frozen)
        recorded_inventory = manifest.get("source_files")
        if inventory != recorded_inventory:
            raise GitCheckpointError("Frozen source differs from the run manifest")
        source_digest = inventory_digest(inventory)
        if source_digest != manifest.get("source_digest"):
            raise GitCheckpointError(
                "Frozen source digest differs from the run manifest"
            )

        if checkpoint_path.is_file():
            existing = _valid_existing(
                project, run_id, source_digest, _read_json(checkpoint_path)
            )
            if existing:
                return existing

        reference = f"refs/heads/{branch}"
        if (
            _git(
                project, ["show-ref", "--verify", "--quiet", reference], check=False
            ).returncode
            == 0
        ):
            raise GitCheckpointError(
                f"Branch {branch} already exists without a matching checkpoint record"
            )

        try:
            code_prefix = code.resolve().relative_to(project).as_posix()
        except ValueError as error:
            raise GitCheckpointError(
                "The code directory is outside the Git project"
            ) from error

        with tempfile.TemporaryDirectory(prefix="experiment-index-") as directory:
            environment = os.environ.copy()
            environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
            _git(project, ["read-tree", base], environment=environment)
            _git(
                project,
                [
                    "rm",
                    "-r",
                    "--cached",
                    # The temporary index holds the launch revision, which may
                    # differ from this checkout's HEAD and files (a later commit,
                    # or a launch from another worktree). Only the index changes.
                    "--force",
                    "--ignore-unmatch",
                    "--",
                    f"{code_prefix}/pipeline",
                    f"{code_prefix}/pixi.toml",
                    f"{code_prefix}/pixi.lock",
                ],
                environment=environment,
            )
            for relative in sorted(inventory):
                source = frozen / relative
                blob = _git(project, ["hash-object", "-w", str(source)]).stdout.strip()
                mode = "100755" if source.stat().st_mode & stat.S_IXUSR else "100644"
                repository_path = f"{code_prefix}/{relative}"
                _git(
                    project,
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        mode,
                        blob,
                        repository_path,
                    ],
                    environment=environment,
                )
            tree = _git(project, ["write-tree"], environment=environment).stdout.strip()
            audit_status = "missing"
            audit_path = result / "audit.json"
            if audit_path.is_file():
                audit_status = str(_read_json(audit_path).get("status", "missing"))
            message = (
                f"experiment: {run_id}\n\n"
                f"Run-ID: {run_id}\n"
                f"Run-State: {terminal.get('state', 'unknown')}\n"
                f"Audit-Status: {audit_status}\n"
                f"Configuration: {manifest.get('configuration', 'unknown')}\n"
                f"Source-Digest: {source_digest}\n"
            )
            commit = _git(
                project,
                ["commit-tree", tree, "-p", base],
                environment=environment,
                input_text=message,
            ).stdout.strip()

        zero_oid = "0" * len(commit)
        _git(project, ["update-ref", reference, commit, zero_oid])
        record = {
            "status": "created",
            "run_id": run_id,
            "branch": branch,
            "commit": commit,
            "base_revision": base,
            "launch_head_when_checkpointed": current_head,
            "source_digest": source_digest,
            "created_utc": _now(),
            "worktree_unchanged": True,
        }
        _write_json(checkpoint_path, record)
        return record
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record = {
            "status": "failed",
            "run_id": run_id,
            "branch": branch,
            "reason": str(error),
            "checked_utc": _now(),
        }
        try:
            if result.is_dir():
                _write_json(checkpoint_path, record)
        except OSError:
            pass
        return record


def require_previous_experiment_checkpoint(code: Path) -> dict[str, Any]:
    """Create or verify the newest experiment checkpoint before new design work."""
    run_id = latest_experiment(code)
    if run_id is None:
        return {"status": "no_previous_experiment"}
    checkpoint = checkpoint_experiment(code, run_id)
    if checkpoint["status"] not in {"created", "existing"}:
        raise GitCheckpointError(
            f"Previous experiment {run_id} is not committed: "
            f"{checkpoint.get('reason', checkpoint['status'])}"
        )
    return checkpoint
