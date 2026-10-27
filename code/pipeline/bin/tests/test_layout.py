import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from research_project import layout


CONTRACT = """
required = ["README.md", "code/pixi.toml"]

[allowed_entries]
"." = [".git", ".gitignore", ".agents", ".claude", "README.md", "CLAUDE.md", "code"]
"code" = ["pixi.toml", "pipeline", "data", "launches", "results", "work", ".worktrees"]

[[placement]]
kind = "Python source"
patterns = ["*.py"]
allowed = ["code/pipeline/bin/"]

[[placement]]
kind = "Data file"
patterns = ["*.csv"]
allowed = ["code/data/"]
"""


def git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".agents/skills/demo").mkdir(parents=True)
    (root / ".agents/layout.toml").write_text(CONTRACT)
    (root / ".agents/skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo.\n---\n"
    )
    (root / ".claude").mkdir()
    (root / ".claude/skills").symlink_to("../.agents/skills")
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "README.md").write_text("# Demo\n")
    (root / ".gitignore").write_text(
        "code/launches/*\n!code/launches/README.md\ncode/results/\ncode/work/\n"
        "code/.worktrees/\n.ruff_cache/\n"
    )
    for directory in (
        "pipeline/bin/research_project",
        "data",
        "launches",
        "results/runs",
    ):
        (root / "code" / directory).mkdir(parents=True)
    (root / "code/pixi.toml").write_text("[workspace]\n")
    (root / "code/launches/README.md").write_text("# Launches\n")
    (root / "code/pipeline/bin/research_project/task.py").write_text("x = 1\n")
    (root / "code/data/input.csv").write_text("a\n1\n")
    return root


def errors(project: Path) -> list[tuple[str, str]]:
    return [
        (item.check, item.path)
        for item in layout.check(project, project / "code")
        if item.severity == "error"
    ]


def test_matching_project_has_no_problems(project: Path) -> None:
    assert errors(project) == []


def test_reports_stray_misplaced_and_missing_paths(project: Path) -> None:
    (project / "notes.txt").write_text("scratch\n")
    (project / "analysis.py").write_text("x = 1\n")
    (project / "code/results.csv").write_text("a\n")
    (project / "README.md").unlink()

    assert sorted(errors(project)) == [
        ("misplaced-file", "analysis.py"),
        ("misplaced-file", "code/results.csv"),
        ("required", "README.md"),
        ("unexpected-entry", "notes.txt"),
    ]


def test_payload_directories_are_not_scanned(project: Path) -> None:
    run = project / "code/results/runs/trial-one"
    run.mkdir(parents=True)
    (run / "table.csv").write_text("a\n")
    (project / "code/launches/trial-one").mkdir()

    assert errors(project) == []


def test_reports_broken_agent_adapters(project: Path) -> None:
    (project / "CLAUDE.md").write_text("Read AGENTS.md\n")
    (project / ".claude/skills").unlink()
    (project / ".claude/skills").mkdir()
    (project / ".agents/skills/demo/SKILL.md").write_text("---\nname: other\n---\n")

    assert sorted(errors(project)) == [
        ("adapter", ".agents/skills/demo"),
        ("adapter", ".claude/skills"),
        ("adapter", "CLAUDE.md"),
    ]


def test_reports_orphaned_and_misplaced_run_records(project: Path) -> None:
    code = project / "code"
    (code / "launches/launch-only").mkdir()
    (code / "results/runs/result-only").mkdir()
    (code / "work").mkdir()
    (code / "work/work-only").mkdir()
    (code / "launches/stray.txt").write_text("x\n")
    (code / ".worktrees/feature/code/results/runs/wt-run").mkdir(parents=True)

    assert sorted(errors(project)) == [
        ("run-record", "code/.worktrees/feature/code/results/runs/wt-run"),
        ("run-record", "code/launches/launch-only"),
        ("run-record", "code/launches/stray.txt"),
        ("run-record", "code/results/runs/result-only"),
        ("run-record", "code/work/work-only"),
    ]


def test_fix_removes_only_caches(project: Path) -> None:
    (project / ".ruff_cache").mkdir()
    (project / "code/pipeline/bin/research_project/__pycache__").mkdir()
    (project / "notes.txt").write_text("keep me\n")
    findings = layout.check(project, project / "code")

    assert {item.path for item in findings if item.check == "cache"} == {
        ".ruff_cache",
        "code/pipeline/bin/research_project/__pycache__",
    }
    assert len(layout.fix(project, findings)) == 2
    assert not (project / ".ruff_cache").exists()
    assert (project / "notes.txt").is_file()


def test_git_checks_tracked_payload_and_worktrees(
    project: Path, tmp_path: Path
) -> None:
    git(project, "init", "-q")
    git(project, "config", "user.name", "Research Test")
    git(project, "config", "user.email", "research@example.invalid")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "initial")
    artifact = project / "code/results/runs/trial-one/out.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("a\n")
    (project / "code/launches/trial-one").mkdir()
    git(project, "add", "-f", str(artifact))
    git(
        project,
        "worktree",
        "add",
        "-q",
        "-b",
        "inside",
        str(project / "code/.worktrees/inside"),
    )
    git(project, "worktree", "add", "-q", "-b", "outside", str(tmp_path / "outside"))
    git(
        project,
        "worktree",
        "add",
        "-q",
        "-b",
        "gone",
        str(project / "code/.worktrees/gone"),
    )
    subprocess.run(["rm", "-rf", str(project / "code/.worktrees/gone")], check=True)
    (project / "code/.worktrees/junk").mkdir()

    findings = layout.check(project, project / "code")

    assert sorted(errors(project)) == [
        ("tracked-payload", "code/results/runs/trial-one/out.csv"),
        ("worktree", str((tmp_path / "outside").resolve())),
        ("worktree", "code/.worktrees/junk"),
    ]
    assert layout.fix(project, findings) == ["Pruned stale worktree metadata"]
    assert "gone" not in git(project, "worktree", "list")


def run_hook(monkeypatch, project: Path, mode: str, event: dict) -> int:
    monkeypatch.setattr(layout, "locate_code_root", lambda: project / "code")
    monkeypatch.setattr(layout, "state_root", lambda code: code)
    monkeypatch.setattr(sys, "argv", ["layout", "--hook", mode])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    return layout.main()


def test_stop_hook_blocks_once_for_turn_problems(project, monkeypatch, capsys) -> None:
    assert run_hook(monkeypatch, project, "stop", {}) == 0

    (project / "scratch.py").write_text("x = 1\n")
    assert run_hook(monkeypatch, project, "stop", {}) == 2
    assert "scratch.py" in capsys.readouterr().err
    assert run_hook(monkeypatch, project, "stop", {"stop_hook_active": True}) == 0


def test_stop_hook_leaves_run_records_to_session_start(
    project, monkeypatch, capsys
) -> None:
    (project / "code/launches/launch-only").mkdir()

    assert run_hook(monkeypatch, project, "stop", {}) == 0
    assert run_hook(monkeypatch, project, "start", {}) == 0
    report = capsys.readouterr().out
    assert "code/launches/launch-only" in report
    assert "cleanup-layout" in report


def test_shipped_contract_parses() -> None:
    root = Path(__file__).resolve().parents[4]
    if not (root / layout.CONTRACT).is_file():
        pytest.skip("Project contract is not present")
    contract = layout.load_contract(root)

    assert contract["required"] and contract["allowed_entries"]["."]
    assert all(rule["patterns"] and rule["allowed"] for rule in contract["placement"])
