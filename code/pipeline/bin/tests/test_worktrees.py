import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from research_project import worktrees
from research_project.runner import git_identity, run, state_root, status


def git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def main(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    code = project / "code"
    (code / "pipeline/conf").mkdir(parents=True)
    (code / "pipeline/conf/trial.config").write_text("// fixture\n")
    (code / "pixi.toml").write_text("[workspace]\n")
    (code / "pixi.lock").write_text("version = 6\n")
    (project / ".gitignore").write_text(
        "code/.worktrees/\ncode/launches/\ncode/results/\ncode/work/\ncode/data/\n"
    )
    git(project, "init", "-q")
    git(project, "config", "user.name", "Research Test")
    git(project, "config", "user.email", "research@example.invalid")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "initial")
    return code.resolve()


def test_worktrees_record_runs_in_the_main_checkout(main: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain/code"
    plain.mkdir(parents=True)
    worktree = worktrees.add(main, "feature", install=False)

    assert worktree == main / ".worktrees/feature"
    assert state_root(main) == main
    assert state_root(worktree / "code") == main
    assert state_root(plain) == plain
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "worktree/feature"


def test_worktree_run_records_launch_and_results_in_main(main: Path) -> None:
    code = worktrees.add(main, "feature", install=False) / "code"

    # The fixture has no Nextflow pipeline, so preparation fails after freezing.
    assert run(code, "trial-one", "trial.config", state=state_root(code)) == 1

    assert status(main, "trial-one")["state"] == "preparation_failed"
    assert (main / "results/runs/trial-one/handoff.md").is_file()
    assert (main / "launches/trial-one/source/code/pixi.toml").is_file()
    execution = (main / "launches/trial-one/execution.config").read_text()
    assert f"data_dir = '{main / 'data'}'" in execution
    assert not (code / "launches").exists()
    assert not (code / "results").exists()


def test_git_identity_records_the_launching_checkout(main: Path) -> None:
    worktree = worktrees.add(main, "feature", install=False)

    identity = git_identity(worktree)

    assert identity["checkout"] == str(worktree)
    assert identity["branch"] == "worktree/feature"
    assert identity["revision"] == git(main, "rev-parse", "HEAD")


def test_remove_keeps_the_branch_and_rejects_other_paths(
    main: Path, tmp_path: Path
) -> None:
    worktree = worktrees.add(main, "feature", install=False)

    with pytest.raises(ValueError, match="already exists"):
        worktrees.add(main, "feature", install=False)
    with pytest.raises(ValueError, match="directly under"):
        worktrees.remove(main, tmp_path / "elsewhere")

    assert worktrees.listed(main) == [
        {"name": "feature", "path": str(worktree), "branch": "worktree/feature"}
    ]
    assert worktrees.remove(main, "feature") == worktree
    assert not worktree.exists()
    assert git(main, "branch", "--list", "worktree/feature")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("Fix Bug #12", "fix-bug-12"), ("agent_a1.b", "agent_a1.b")],
)
def test_slug_normalizes_names(requested: str, expected: str) -> None:
    assert worktrees.slug(requested) == expected


@pytest.mark.parametrize("requested", ["../..", "", "///"])
def test_slug_rejects_unusable_names(requested: str) -> None:
    with pytest.raises(ValueError):
        worktrees.slug(requested)


def test_claude_hooks_create_and_remove(main: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(worktrees, "locate_code_root", lambda: main)
    monkeypatch.setattr(worktrees, "install_environment", lambda path: None)

    def hook(action: str, payload: dict) -> int:
        monkeypatch.setattr(sys, "argv", ["worktrees", action])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        return worktrees.main()

    assert hook("hook-create", {"name": "Agent Task"}) == 0
    created = Path(capsys.readouterr().out.strip())
    assert created == main / ".worktrees/agent-task"
    assert created.is_dir()

    assert hook("hook-remove", {"worktree_path": str(created)}) == 0
    assert not created.exists()


def test_add_reuses_an_existing_branch_but_not_with_a_base(main: Path) -> None:
    git(main.parent, "branch", "analysis")

    with pytest.raises(ValueError, match="already exists; omit --base"):
        worktrees.add(main, "analysis", branch="analysis", base="HEAD", install=False)
    worktree = worktrees.add(main, "analysis", branch="analysis", install=False)

    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "analysis"


def test_add_from_a_worktree_places_the_new_one_in_the_main_checkout(
    main: Path,
) -> None:
    first = worktrees.add(main, "first", install=False)
    (first / "code/pipeline/conf/trial.config").write_text("// first change\n")
    git(first, "commit", "-q", "-am", "first change")

    second = worktrees.add(first / "code", "second", install=False)

    assert second == main / ".worktrees/second"
    assert git(second, "rev-parse", "HEAD") == git(first, "rev-parse", "HEAD")


def test_remove_refuses_uncommitted_work(main: Path) -> None:
    worktree = worktrees.add(main, "feature", install=False)
    (worktree / "notes.txt").write_text("unsaved\n")

    with pytest.raises(ValueError):
        worktrees.remove(main, "feature")
    assert (worktree / "notes.txt").is_file()


def test_remove_explains_an_initialized_submodule(main: Path, tmp_path: Path) -> None:
    paper = tmp_path / "paper.git"
    git(tmp_path, "init", "-q", "--bare", str(paper))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(paper), str(seed))
    (seed / "README.md").write_text("# Paper\n")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "HEAD")
    project = main.parent
    local = ["-c", "protocol.file.allow=always"]
    git(project, *local, "submodule", "add", "-q", str(paper), "manuscript")
    git(project, "commit", "-q", "-m", "Add manuscript")
    worktree = worktrees.add(main, "feature", install=False)
    git(worktree, *local, "submodule", "update", "-q", "--init")

    with pytest.raises(ValueError, match="--force"):
        worktrees.remove(main, "feature")
    assert (worktree / "manuscript/README.md").is_file()
