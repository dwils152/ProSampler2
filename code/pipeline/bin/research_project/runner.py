"""Run bounded Nextflow experiments and retain auditable evidence."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .example import audit_example
from .catalog_io import add_note, atomic_text, automatic_refresh
from .paths import locate_code_root, state_root  # noqa: F401 (re-exported)
from .git_checkpoint import (
    checkpoint_experiment,
    require_git_repository,
    require_previous_experiment_checkpoint,
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_digest(inventory: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def run_paths(code: Path, run_id: str) -> tuple[Path, Path, Path]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        raise ValueError("Run ID must use 1–64 lowercase letters, digits or hyphens")
    return (
        code / "launches" / run_id,
        code / "results/runs" / run_id,
        code / "work" / run_id,
    )


def inside(directory: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Expected a contained relative path: {relative}")
    resolved = (directory / path).resolve()
    resolved.relative_to(directory.resolve())
    return resolved


def source_inventory(code: Path) -> dict[str, str]:
    excluded = {"__pycache__", ".pytest_cache", ".ruff_cache", "dist", ".pixi"}
    paths = [code / "pixi.toml", code / "pixi.lock"]
    for path in sorted((code / "pipeline").rglob("*")):
        if excluded.intersection(path.relative_to(code).parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(
                f"Source snapshots require regular files, not symlinks: {path}"
            )
        if path.is_file():
            paths.append(path)
    return {str(path.relative_to(code)): sha256(path) for path in paths}


def native_string(value: str) -> str:
    return (
        "'"
        + value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + "'"
    )


def validate_parameters(params: dict[str, Any]) -> float:
    timeout = params.get("driver_timeout_seconds")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("driver_timeout_seconds must be a positive, finite number")
    outputs = params.get("expected_outputs")
    if (
        not isinstance(outputs, list)
        or not outputs
        or not all(isinstance(item, str) and item for item in outputs)
    ):
        raise ValueError("expected_outputs must contain nonempty relative paths")
    for output in outputs:
        if Path(output).is_absolute() or ".." in Path(output).parts:
            raise ValueError("expected_outputs must stay inside the run record")
    if params.get("workflow_name") != "example":
        raise ValueError(
            "Add an explicit output audit before registering another workflow"
        )
    if type(params.get("seed")) is not int:
        raise ValueError("seed must be an integer")
    if (
        type(params.get("sample_count")) is not int
        or not 1 <= params["sample_count"] <= 10000
    ):
        raise ValueError("sample_count must be an integer between 1 and 10000")
    return float(timeout)


def git_identity(project: Path) -> dict[str, Any]:
    if shutil.which("git") is None:
        return {"revision": None, "dirty": None}
    # Exported templates and sandbox mounts can contain a .git placeholder
    # without a usable repository. Frozen file hashes remain authoritative.
    inside_tree = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if inside_tree.returncode or inside_tree.stdout.strip() != "true":
        return {
            "revision": None,
            "dirty": None,
            "unavailable_reason": "Not a usable Git working tree",
        }
    revision = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    dirty = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    branch = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    identity = {
        "revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "checkout": str(project.resolve()),
    }
    if dirty.returncode:
        identity["unavailable_reason"] = (
            "Git status unavailable; consult frozen source hashes"
        )
    return identity


def execute(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    log: Path,
) -> tuple[str, int]:
    with log.open("w") as output:
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process:
            try:
                code = process.wait(timeout=timeout)
                return ("completed" if code == 0 else "failed"), code
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return (
                    ("timed_out", 124)
                    if isinstance(error, subprocess.TimeoutExpired)
                    else ("interrupted", 130)
                )


def status(code: Path, run_id: str) -> dict[str, Any]:
    _, result, _ = run_paths(code, run_id)
    if not result.is_dir():
        raise ValueError(f"No run record exists for {run_id}")
    terminal = result / "terminal.json"
    record = {
        "run_id": run_id,
        "result_dir": str(result),
        **(read_json(terminal) if terminal.exists() else {"state": "preparing"}),
    }
    checkpoint = result / "git-checkpoint.json"
    if checkpoint.is_file():
        record["git_checkpoint"] = read_json(checkpoint)
    return record


def audit(code: Path, run_id: str) -> dict[str, Any]:
    launch, result, _ = run_paths(code, run_id)
    errors: list[str] = []
    hashes: dict[str, str] = {}
    try:
        manifest = read_json(result / "manifest.json")
        terminal = read_json(result / "terminal.json")
        if terminal.get("state") != "completed" or terminal.get("exit_code") != 0:
            errors.append("Driver did not complete successfully")
        source = launch / "source/code"
        if source_inventory(source) != manifest["source_files"]:
            errors.append("Frozen source inventory changed")
        if inventory_digest(manifest["source_files"]) != manifest["source_digest"]:
            errors.append("Source digest does not match the inventory")
        for name in ("execution.config", "resolved-config.json"):
            if sha256(result / name) != manifest["record_hashes"][name]:
                errors.append(f"Recorded configuration changed: {name}")
        if (
            sha256(launch / "execution.config")
            != manifest["record_hashes"]["execution.config"]
        ):
            errors.append("Launch configuration changed")
        params = manifest["parameters"]
        validate_parameters(params)
        if read_json(result / "resolved-config.json")["params"] != params:
            errors.append("Manifest parameters differ from the resolved configuration")
        for name in params["expected_outputs"]:
            artifact = inside(result, name)
            if not artifact.is_file():
                errors.append(f"Missing expected artifact: {name}")
            else:
                hashes[name] = sha256(artifact)
        with (result / "trace.tsv").open(newline="") as handle:
            tasks = list(csv.DictReader(handle, delimiter="\t"))
        expected_names = {"EXAMPLE:GENERATE_SAMPLES", "EXAMPLE:SUMMARIZE_SAMPLES"}
        if (
            len(tasks) != 2
            or {task["name"].split(" (", 1)[0] for task in tasks} != expected_names
        ):
            errors.append("Trace does not contain the two expected example processes")
        if any(
            task["status"] not in {"COMPLETED", "CACHED"} or task["exit"] != "0"
            for task in tasks
        ):
            errors.append("Trace contains unsuccessful tasks")
        audit_example(
            result / "example",
            seed=params["seed"],
            count=params["sample_count"],
            require_parquet="example/samples.parquet" in params["expected_outputs"],
            parquet_threads=params.get("parquet_threads"),
            parquet_memory_limit=params.get("parquet_memory_limit"),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    return {
        "run_id": run_id,
        "status": "fail" if errors else "pass",
        "errors": errors,
        "artifact_sha256": hashes,
    }


def handoff(
    code: Path, run_id: str, *, objective: str, next_step: str, notes: str = ""
) -> Path:
    _, result, _ = run_paths(code, run_id)
    recorded = status(code, run_id)
    note = result / "handoff.md"
    body = (
        f"# Run handoff — {run_id}\n\nUpdated — {now()}\n\n"
        f"Objective — {objective}\n\nRecorded state — {recorded['state']}\n\n"
        f"Next action — {next_step}\n\nNotes — {notes or 'None recorded.'}\n\n"
        "Evidence — [manifest](manifest.json), [terminal state](terminal.json), "
        "[audit](audit.json), [driver log](driver.log), [trace](trace.tsv). "
        "Some artifacts may be absent after a failed preparation.\n\n"
        "Refresh recorded and live scheduler status before resuming. "
        "This note does not authorize another launch.\n"
    )
    previous = result / "handoff.json"
    supersedes = read_json(previous).get("note_id") if previous.exists() else None
    if not previous.exists() and note.exists():
        legacy = add_note(
            code, run_id, kind="handoff", text=note.read_text(), author="unrecorded"
        )
        supersedes = legacy["note_id"]
    # Keep each research-state revision while handoff.md shows the newest one.
    revision = add_note(
        code, run_id, kind="handoff", text=body, author="runner", supersedes=supersedes
    )
    write_json(previous, revision)
    atomic_text(note, body)
    return note


def latest_handoff(code: Path, state: Path | None = None) -> str:
    notes = list(((state or code) / "results/runs").glob("*/handoff.md"))
    if notes:
        note = max(notes, key=lambda item: item.stat().st_mtime_ns)
        return f"Saved project handoff ({note})\n\n{note.read_text()}"
    note = code / "docs/session.md"
    return note.read_text() if note.exists() else "No handoff has been recorded."


def run(
    code: Path,
    run_id: str,
    configuration: str,
    profile: str = "local",
    *,
    git_checkpoints: bool = False,
    run_kind: str = "experiment",
    state: Path | None = None,
) -> int:
    """Run the source in ``code`` and record it under ``state`` (default ``code``)."""
    state = state or code
    if profile not in {"local", "slurm"}:
        raise ValueError("Profile must be local or slurm")
    if run_kind not in {"experiment", "validation"}:
        raise ValueError("Run kind must be experiment or validation")
    if git_checkpoints:
        require_git_repository(code.parent)
        require_previous_experiment_checkpoint(state)
    config = inside(code / "pipeline/conf", configuration)
    if config.suffix != ".config" or not config.is_file():
        raise ValueError("Choose an existing native configuration under pipeline/conf/")
    launch, result, work = run_paths(state, run_id)
    if any(path.exists() for path in (launch, result, work)):
        raise ValueError(
            f"Run ID {run_id} already exists; refusing to overwrite or duplicate it"
        )
    # mkdir without exist_ok atomically reserves this ID for one writer.
    launch.mkdir(parents=True)
    result.mkdir(parents=True)
    write_json(result / "terminal.json", {"state": "preparing", "started_utc": now()})
    outcome, exit_code = "preparation_failed", 1
    try:
        inventory = source_inventory(code)
        source = launch / "source/code"
        for relative in inventory:
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(code / relative, destination)
        if source_inventory(source) != inventory:
            raise ValueError("Source changed while the snapshot was being copied")
        execution = launch / "execution.config"
        frozen_config = source / config.relative_to(code)
        execution.write_text(
            f"includeConfig {native_string(str(frozen_config))}\n"
            "params {\n"
            f"    run_id = {native_string(run_id)}\n"
            f"    outdir = {native_string(str(result))}\n"
            f"    data_dir = {native_string(str(state / 'data'))}\n"
            "}\n"
        )
        with execution.open("a") as handle:
            for kind, filename in (
                ("trace", "trace.tsv"),
                ("report", "execution-report.html"),
                ("timeline", "timeline.html"),
            ):
                handle.write(
                    f"{kind} {{\n    enabled = true\n"
                    f"    file = {native_string(str(result / filename))}\n"
                    "    overwrite = false\n"
                )
                if kind == "trace":
                    handle.write(
                        "    fields = 'task_id,hash,native_id,name,status,exit,cpus,submit,start,complete,duration,realtime,peak_rss,attempt'\n"
                    )
                handle.write("}\n")
        shutil.copy2(execution, result / "execution.config")
        environment = {
            **os.environ,
            "NXF_SYNTAX_PARSER": "v2",
            "NXF_HOME": str(launch / "nextflow-home"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(source / "pipeline/bin"),
        }
        fixed_configs = f"{source / 'pipeline/nextflow.config'},{execution}"
        render = [
            "nextflow",
            "-C",
            fixed_configs,
            "config",
            str(source / "pipeline"),
            "-profile",
            profile,
            "-o",
            "json",
        ]
        rendered = subprocess.run(
            render,
            cwd=launch,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        (result / "config-render.log").write_text(rendered.stderr)
        if rendered.returncode:
            (result / "config-render.log").write_text(rendered.stderr + rendered.stdout)
            raise ValueError("Nextflow configuration failed; see config-render.log")
        resolved = json.loads(rendered.stdout)
        timeout = validate_parameters(resolved["params"])
        write_json(result / "resolved-config.json", resolved)
        command = [
            "nextflow",
            "-log",
            str(result / "nextflow.log"),
            "-C",
            fixed_configs,
            "run",
            str(source / "pipeline/main.nf"),
            "-profile",
            profile,
            "-work-dir",
            str(work),
            "-ansi-log",
            "false",
        ]
        manifest = {
            "run_id": run_id,
            "run_kind": run_kind,
            "created_utc": now(),
            "configuration": str(config.relative_to(code)),
            "profile": profile,
            "parameters": resolved["params"],
            "command": command,
            "source_digest": inventory_digest(inventory),
            "source_files": inventory,
            "record_hashes": {
                name: sha256(result / name)
                for name in ("execution.config", "resolved-config.json")
            },
            "git": git_identity(code.parent),
            "python": sys.executable,
            "python_version": sys.version,
            "environment_lock_sha256": inventory["pixi.lock"],
            "hostname": socket.gethostname(),
        }
        write_json(result / "manifest.json", manifest)
        write_json(
            result / "terminal.json",
            {
                "state": "running",
                "driver_pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_utc": now(),
                "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            },
        )
        outcome, exit_code = execute(
            command,
            cwd=launch,
            environment=environment,
            timeout=timeout,
            log=result / "driver.log",
        )
        write_json(
            result / "terminal.json",
            {
                "state": outcome,
                "exit_code": exit_code,
                "finished_utc": now(),
                "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            },
        )
        evidence = audit(state, run_id)
        write_json(result / "audit.json", evidence)
        if exit_code == 0 and evidence["status"] != "pass":
            outcome, exit_code = "audit_failed", 1
            write_json(
                result / "terminal.json",
                {
                    "state": outcome,
                    "exit_code": exit_code,
                    "finished_utc": now(),
                    "errors": evidence["errors"],
                },
            )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        outcome = (
            "interrupted"
            if isinstance(error, KeyboardInterrupt)
            else "preparation_failed"
        )
        exit_code = 130 if isinstance(error, KeyboardInterrupt) else 1
        write_json(
            result / "terminal.json",
            {
                "state": outcome,
                "exit_code": exit_code,
                "finished_utc": now(),
                "error": str(error),
            },
        )
        print(f"{outcome}: {error}", file=sys.stderr)
    handoff(
        state,
        run_id,
        objective=f"Execute {configuration} with the {profile} profile",
        next_step="Review the recorded audit and outputs"
        if outcome == "completed"
        else "Inspect the failure record and any live scheduler jobs before planning a correction",
    )
    automatic_refresh(state, run_id)
    if git_checkpoints:
        checkpoint = checkpoint_experiment(state, run_id)
        if checkpoint["status"] not in {"created", "existing"}:
            print(
                f"Git checkpoint pending for {run_id}: "
                f"{checkpoint.get('reason', checkpoint['status'])}",
                file=sys.stderr,
            )
    print(json.dumps(status(state, run_id), indent=2))
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    execution = commands.add_parser("run")
    execution.add_argument("--run-id", required=True)
    execution.add_argument("--config", required=True)
    execution.add_argument("--profile", choices=["local", "slurm"], default="local")
    for name in ("status", "audit", "handoff"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name == "handoff":
            command.add_argument("--objective", required=True)
            command.add_argument("--next-step", required=True)
            command.add_argument("--notes", default="")
    for name in ("smoke", "verify", "latest-handoff"):
        commands.add_parser(name)
    args = parser.parse_args()
    try:
        code = locate_code_root()
        state = state_root(code)
        if args.action == "run":
            raise SystemExit(
                run(
                    code,
                    args.run_id,
                    args.config,
                    args.profile,
                    git_checkpoints=True,
                    state=state,
                )
            )
        if args.action in {"smoke", "verify"}:
            if args.action == "verify":
                check = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "-c",
                        "pipeline/bin/pyproject.toml",
                        "pipeline/bin/tests",
                    ],
                    cwd=code,
                )
                if check.returncode:
                    raise SystemExit(check.returncode)
            run_id = (
                "smoke-"
                + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-")
                + uuid.uuid4().hex[:8]
            )
            exit_code = run(
                code, run_id, "smoke.config", run_kind="validation", state=state
            )
            if exit_code == 0:
                # Validation commands require the optional catalog integration
                # to work, while ordinary experiments retain their own outcome.
                from .catalog import get_evidence

                _, result, _ = run_paths(state, run_id)
                try:
                    indexed = get_evidence(state, run_id)
                    params = read_json(result / "manifest.json")["parameters"]
                    expected = {spec["name"] for spec in params["catalog_metrics"]}
                    if (
                        indexed["run"]["errors_json"]
                        or indexed["run"]["state"] != "completed"
                        or indexed["run"]["audit_status"] != "pass"
                        or {metric["name"] for metric in indexed["metrics"]} != expected
                        or any(metric["value"] is None for metric in indexed["metrics"])
                        or {
                            item["path"]
                            for item in indexed["artifacts"]
                            if item["present"]
                        }
                        != set(params["expected_outputs"])
                    ):
                        raise ValueError(
                            "Catalog does not contain the complete audited smoke run"
                        )
                    check = {
                        "status": "pass",
                        "run_id": run_id,
                        "snapshot_id": indexed["catalog"]["snapshot_id"],
                        "metrics": sorted(expected),
                        "checked_utc": now(),
                    }
                except Exception as error:
                    check = {
                        "status": "fail",
                        "run_id": run_id,
                        "error": str(error),
                        "checked_utc": now(),
                    }
                    exit_code = 1
                write_json(result / "catalog-validation.json", check)
                print(json.dumps(check, indent=2))
            raise SystemExit(exit_code)
        if args.action == "status":
            print(json.dumps(status(state, args.run_id), indent=2))
        elif args.action == "audit":
            evidence = audit(state, args.run_id)
            print(json.dumps(evidence, indent=2))
            raise SystemExit(0 if evidence["status"] == "pass" else 1)
        elif args.action == "handoff":
            print(
                handoff(
                    state,
                    args.run_id,
                    objective=args.objective,
                    next_step=args.next_step,
                    notes=args.notes,
                )
            )
            automatic_refresh(state, args.run_id)
        else:
            print(latest_handoff(code, state))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
