import io
import json
import subprocess
from pathlib import Path

import pytest

from research_project import launch_hook


PREFIX = "pixi run --locked --manifest-path code/pixi.toml"


def event(command: str | list[str], tool_name: str = "Bash") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *arguments], capture_output=True, check=True
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Research Test")
    git(tmp_path, "config", "user.email", "research@example.invalid")
    (tmp_path / "trial.config").write_text("params.seed = 1\n")
    git(tmp_path, "add", "trial.config")
    git(tmp_path, "commit", "-q", "-m", "Initial")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        f"{PREFIX} experiment --run-id trial-001 --config example.config",
        "cd code && pixi run -e default experiment --run-id trial-001",
        "pixi run --manifest-path=code/pixi.toml experiment --run-id trial-001",
        "timeout 600 /opt/pixi/bin/pixi run experiment --run-id trial-001",
        f"{PREFIX} python -m research_project.runner run --run-id trial-001",
        "bash -lc 'cd code && pixi run experiment --run-id trial-001'",
        f"{PREFIX} status --run-id trial-000\n{PREFIX} experiment --run-id t-1",
    ],
)
def test_detects_unacknowledged_experiment_launches(command: str) -> None:
    assert launch_hook.experiment_launches(command) == [False]


@pytest.mark.parametrize(
    "command",
    [
        f"{PREFIX} status --run-id experiment-001",
        f"{PREFIX} audit --run-id experiment-001",
        f"{PREFIX} smoke",
        f"{PREFIX} verify",
        'git commit -m "Prepare experiment run"',
        "ls code/results/runs/experiment-001",
    ],
)
def test_ignores_commands_that_do_not_launch(command: str) -> None:
    assert launch_hook.experiment_launches(command) == []


def test_acknowledgement_applies_only_to_its_own_command() -> None:
    assert launch_hook.experiment_launches(
        f"ALLOW_DIRTY_LAUNCH=1 {PREFIX} experiment --run-id trial-001"
    ) == [True]
    assert launch_hook.experiment_launches(
        "env ALLOW_DIRTY_LAUNCH=1 bash -c 'pixi run experiment --run-id t-1'"
    ) == [True]
    assert launch_hook.experiment_launches(
        f"ALLOW_DIRTY_LAUNCH=1 true; {PREFIX} experiment --run-id trial-001"
    ) == [False]


def test_pauses_a_launch_on_a_dirty_tree(project: Path) -> None:
    (project / "trial.config").write_text("params.seed = 2\n")
    (project / "notes.txt").write_text("draft\n")

    message = launch_hook.check(event(f"{PREFIX} experiment --run-id t-1"), project)

    assert message is not None
    assert " M trial.config" in message
    assert "?? notes.txt" in message
    assert "Ask the user whether to commit" in message
    assert launch_hook.ACKNOWLEDGEMENT in message


def test_allows_clean_acknowledged_and_non_launch_commands(project: Path) -> None:
    launch = f"{PREFIX} experiment --run-id t-1"
    assert launch_hook.check(event(launch), project) is None

    (project / "trial.config").write_text("params.seed = 2\n")
    assert launch_hook.check(event(f"ALLOW_DIRTY_LAUNCH=1 {launch}"), project) is None
    assert launch_hook.check(event(f"{PREFIX} status --run-id t-1"), project) is None
    assert launch_hook.check(event(launch, tool_name="Edit"), project) is None


def test_reads_argument_vector_commands(project: Path) -> None:
    (project / "trial.config").write_text("params.seed = 2\n")
    wrapped = ["bash", "-lc", f"{PREFIX} experiment --run-id t-1"]

    assert launch_hook.check(event(wrapped), project) is not None


def test_leaves_launch_to_the_runner_without_git(tmp_path: Path) -> None:
    assert (
        launch_hook.check(event(f"{PREFIX} experiment --run-id t-1"), tmp_path) is None
    )


def test_hook_exit_codes(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(launch_hook, "PROJECT", project)
    (project / "trial.config").write_text("params.seed = 2\n")

    def run(text: str) -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        return launch_hook.main()

    assert run(json.dumps(event(f"{PREFIX} experiment --run-id t-1"))) == 2
    assert "Experiment launch paused" in capsys.readouterr().err
    assert run(json.dumps(event("ls"))) == 0
    assert run("not json") == 0
    assert "Launch check skipped" in capsys.readouterr().out
