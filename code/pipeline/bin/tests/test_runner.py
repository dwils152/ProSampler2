import json
import os
import sys
import subprocess
from pathlib import Path

import pytest

from research_project.example import generate, summarize
from research_project.runner import (
    audit,
    execute,
    git_identity,
    handoff,
    inside,
    inventory_digest,
    latest_handoff,
    run,
    run_paths,
    sha256,
    source_inventory,
    status,
    validate_parameters,
    write_json,
)


def parameters() -> dict:
    return {
        "workflow_name": "example",
        "seed": 42,
        "sample_count": 8,
        "driver_timeout_seconds": 60,
        "expected_outputs": ["example/samples.csv", "example/summary.json"],
    }


def fixture_run(code: Path) -> Path:
    launch, result, _ = run_paths(code, "example-001")
    source = launch / "source/code"
    (source / "pipeline").mkdir(parents=True)
    (source / "pixi.toml").write_text("# fixture\n")
    (source / "pixi.lock").write_text("# fixture\n")
    (source / "pipeline/main.nf").write_text("// fixture\n")
    (result / "example").mkdir(parents=True)
    (launch / "execution.config").write_text("// fixture\n")
    (result / "execution.config").write_text("// fixture\n")
    write_json(result / "resolved-config.json", {"params": parameters()})
    inventory = source_inventory(source)
    write_json(
        result / "manifest.json",
        {
            "parameters": parameters(),
            "source_files": inventory,
            "source_digest": inventory_digest(inventory),
            "record_hashes": {
                name: sha256(result / name)
                for name in ("execution.config", "resolved-config.json")
            },
        },
    )
    write_json(result / "terminal.json", {"state": "completed", "exit_code": 0})
    (result / "trace.tsv").write_text(
        "name\tstatus\texit\nEXAMPLE:GENERATE_SAMPLES\tCOMPLETED\t0\nEXAMPLE:SUMMARIZE_SAMPLES\tCOMPLETED\t0\n"
    )
    generate(42, 8, result / "example/samples.csv")
    summarize(result / "example/samples.csv", result / "example/summary.json")
    return result


@pytest.mark.parametrize("identifier", ["../escape", "/tmp/run", "UPPER", "", "a" * 65])
def test_invalid_run_ids_cannot_escape(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError):
        run_paths(tmp_path, identifier)


def test_paths_reject_traversal_and_symlink_escape(tmp_path: Path) -> None:
    directory = tmp_path / "record"
    directory.mkdir()
    (directory / "escape").symlink_to(tmp_path, target_is_directory=True)
    for name in ("../other", "/tmp/other", "escape/other"):
        with pytest.raises(ValueError):
            inside(directory, name)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "60"])
def test_unbounded_or_invalid_timeout_rejected(timeout: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_parameters({**parameters(), "driver_timeout_seconds": timeout})


def test_existing_run_is_preserved(tmp_path: Path) -> None:
    (tmp_path / "pipeline/conf").mkdir(parents=True)
    (tmp_path / "pipeline/conf/example.config").write_text("// fixture")
    launch, _, _ = run_paths(tmp_path, "existing")
    launch.mkdir(parents=True)
    marker = launch / "keep.txt"
    marker.write_text("user work")
    with pytest.raises(ValueError, match="already exists"):
        run(tmp_path, "existing", "example.config")
    assert marker.read_text() == "user work"


def test_unusable_git_metadata_keeps_hash_based_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "research_project.runner.shutil.which", lambda name: "/usr/bin/git"
    )
    monkeypatch.setattr(
        "research_project.runner.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 128, stdout="", stderr="not a repository"
        ),
    )
    identity = git_identity(tmp_path)
    assert identity["revision"] is None and identity["dirty"] is None
    assert identity["unavailable_reason"]


def test_configuration_escape_creates_no_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run(tmp_path, "bad-config", "../../../secret.config")
    assert not (tmp_path / "launches").exists()


def test_missing_lockfile_records_preparation_failure(tmp_path: Path) -> None:
    (tmp_path / "pipeline/conf").mkdir(parents=True)
    (tmp_path / "pipeline/conf/example.config").write_text("// fixture")
    (tmp_path / "pixi.toml").write_text("# fixture")
    assert run(tmp_path, "no-lock", "example.config") == 1
    assert status(tmp_path, "no-lock")["state"] == "preparation_failed"
    assert (tmp_path / "results/runs/no-lock/handoff.md").is_file()


def test_audit_verifies_source_trace_and_content(tmp_path: Path) -> None:
    result = fixture_run(tmp_path)
    assert audit(tmp_path, "example-001")["status"] == "pass"
    source = tmp_path / "launches/example-001/source/code/pipeline/main.nf"
    source.write_text("// changed after launch")
    assert "Frozen source inventory changed" in audit(tmp_path, "example-001")["errors"]
    (result / "example/summary.json").unlink()
    assert any(
        "Missing expected artifact" in message
        for message in audit(tmp_path, "example-001")["errors"]
    )


def test_failed_driver_is_not_scientific_success(tmp_path: Path) -> None:
    result = fixture_run(tmp_path)
    write_json(result / "terminal.json", {"state": "failed", "exit_code": 1})
    assert audit(tmp_path, "example-001")["status"] == "fail"


def test_missing_task_rejected_even_with_valid_outputs(tmp_path: Path) -> None:
    result = fixture_run(tmp_path)
    (result / "trace.tsv").write_text(
        "name\tstatus\texit\nEXAMPLE:GENERATE_SAMPLES\tCOMPLETED\t0\n"
    )
    assert audit(tmp_path, "example-001")["status"] == "fail"


def test_handoff_retains_user_intent_and_recorded_state(tmp_path: Path) -> None:
    fixture_run(tmp_path)
    note = handoff(
        tmp_path,
        "example-001",
        objective="Inspect pairing",
        next_step="Check specimen IDs",
        notes="Unit tests passed; HPC not checked.",
    )
    assert "Inspect pairing" in note.read_text()
    assert "Check specimen IDs" in latest_handoff(tmp_path)
    assert "completed" in note.read_text()


def test_execute_records_failure_and_enforces_timeout(tmp_path: Path) -> None:
    arguments = {
        "cwd": tmp_path,
        "environment": dict(os.environ),
        "log": tmp_path / "driver.log",
    }
    assert execute(
        [sys.executable, "-c", "raise SystemExit(3)"], timeout=10, **arguments
    ) == ("failed", 3)
    assert execute(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1, **arguments
    ) == ("timed_out", 124)
