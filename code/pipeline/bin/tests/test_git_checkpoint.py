import json
import shutil
import subprocess
from pathlib import Path

import pytest

from research_project.git_checkpoint import (
    GitCheckpointError,
    checkpoint_experiment,
    latest_experiment,
    require_git_repository,
    require_previous_experiment_checkpoint,
)
from research_project.runner import (
    inventory_digest,
    run,
    run_paths,
    source_inventory,
    status,
    write_json,
)


def git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def recorded_project(
    tmp_path: Path, run_id: str = "trial-one"
) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    code = project / "code"
    (code / "pipeline").mkdir(parents=True)
    (code / "pipeline/main.nf").write_text("// frozen experiment\n")
    (code / "pixi.toml").write_text("[workspace]\n")
    (code / "pixi.lock").write_text("version = 6\n")
    git(project, "init", "-q")
    git(project, "config", "user.name", "Research Test")
    git(project, "config", "user.email", "research@example.invalid")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "initial")
    base = git(project, "rev-parse", "HEAD")

    launch, result, _ = run_paths(code, run_id)
    frozen = launch / "source/code"
    inventory = source_inventory(code)
    for relative in inventory:
        destination = frozen / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(code / relative, destination)
    result.mkdir(parents=True)
    write_json(
        result / "manifest.json",
        {
            "run_id": run_id,
            "run_kind": "experiment",
            "created_utc": "2026-01-01T00:00:00+00:00",
            "configuration": "pipeline/conf/trial.config",
            "source_files": inventory,
            "source_digest": inventory_digest(inventory),
            "git": {"revision": base, "dirty": False},
        },
    )
    write_json(result / "terminal.json", {"state": "completed", "exit_code": 0})
    write_json(result / "audit.json", {"status": "pass", "errors": []})
    return project, code, base


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_checkpoint_uses_frozen_source_without_switching_worktree(
    tmp_path: Path,
) -> None:
    project, code, base = recorded_project(tmp_path)
    branch_before = git(project, "branch", "--show-current")
    (code / "pipeline/main.nf").write_text("// later design\n")
    worktree_before = git(project, "status", "--porcelain")

    checkpoint = checkpoint_experiment(code, "trial-one")

    assert checkpoint["status"] == "created"
    assert checkpoint["branch"] == "experiment/trial-one"
    assert git(project, "branch", "--show-current") == branch_before
    assert git(project, "status", "--porcelain") == worktree_before
    assert (
        git(project, "show", "experiment/trial-one:code/pipeline/main.nf")
        == "// frozen experiment"
    )
    assert git(project, "rev-parse", "experiment/trial-one^") == base
    assert status(code, "trial-one")["git_checkpoint"]["commit"] == checkpoint["commit"]

    repeated = checkpoint_experiment(code, "trial-one")
    assert repeated["status"] == "existing"
    assert repeated["commit"] == checkpoint["commit"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_checkpoint_after_head_moved_or_from_another_worktree(tmp_path: Path) -> None:
    # A later commit (or a launch from a linked worktree) leaves the checkout's
    # HEAD and files different from the launch revision in the temporary index.
    project, code, base = recorded_project(tmp_path)
    (code / "pipeline/main.nf").write_text("// later design\n")
    git(project, "commit", "-qam", "later design")
    before = git(project, "status", "--porcelain")

    checkpoint = checkpoint_experiment(code, "trial-one")

    assert checkpoint["status"] == "created", checkpoint.get("reason")
    assert git(project, "rev-parse", "experiment/trial-one^") == base
    assert (
        git(project, "show", "experiment/trial-one:code/pipeline/main.nf")
        == "// frozen experiment"
    )
    assert git(project, "status", "--porcelain") == before


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_previous_experiment_is_checkpointed_and_validation_runs_are_ignored(
    tmp_path: Path,
) -> None:
    project, code, _ = recorded_project(tmp_path)
    validation = code / "results/runs/smoke-newer"
    validation.mkdir(parents=True)
    write_json(
        validation / "manifest.json",
        {
            "run_id": "smoke-newer",
            "run_kind": "validation",
            "created_utc": "2027-01-01T00:00:00+00:00",
        },
    )

    assert latest_experiment(code) == "trial-one"
    checkpoint = require_previous_experiment_checkpoint(code)
    assert checkpoint["status"] == "created"
    assert git(project, "rev-parse", "--verify", "experiment/trial-one")


def test_launch_preflight_requires_a_repository_with_an_initial_commit(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitCheckpointError, match="Initialize"):
        require_git_repository(tmp_path)


def test_experiment_launch_checks_git_before_reserving_run_id(tmp_path: Path) -> None:
    code = tmp_path / "code"
    (code / "pipeline/conf").mkdir(parents=True)
    (code / "pipeline/conf/trial.config").write_text("// fixture\n")

    with pytest.raises(GitCheckpointError, match="Initialize"):
        run(
            code,
            "trial-one",
            "trial.config",
            git_checkpoints=True,
        )

    assert not (code / "launches").exists()
    assert not (code / "results/runs/trial-one").exists()
