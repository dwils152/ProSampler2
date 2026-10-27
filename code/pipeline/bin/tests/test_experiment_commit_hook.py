from pathlib import Path

from research_project.experiment_commit_hook import (
    affects_experiment_design,
    changed_paths,
)


def event(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": command},
    }


def claude_event(tool_name: str, file_path: Path) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)},
    }


def test_detects_experiment_design_paths(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    paths = changed_paths(
        event(
            "*** Begin Patch\n"
            "*** Update File: code/pipeline/conf/trial.config\n"
            "*** Update File: README.md\n"
            "*** End Patch"
        ),
        code,
    )

    assert paths == [Path("pipeline/conf/trial.config")]
    assert affects_experiment_design(paths)


def test_ignores_non_design_edits_and_other_tools(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    readme = changed_paths(
        event("*** Begin Patch\n*** Update File: README.md\n*** End Patch"), code
    )
    other = changed_paths(
        {
            **event(
                "*** Begin Patch\n*** Update File: code/pipeline/main.nf\n*** End Patch"
            ),
            "tool_name": "Bash",
        },
        code,
    )

    assert not affects_experiment_design(readme)
    assert other == []


def test_detects_claude_code_design_edits(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()

    for tool_name in ("Edit", "MultiEdit", "Write"):
        paths = changed_paths(claude_event(tool_name, code / "pipeline/main.nf"), code)
        assert paths == [Path("pipeline/main.nf")]
        assert affects_experiment_design(paths)

    assert not affects_experiment_design(
        changed_paths(claude_event("Write", tmp_path / "README.md"), code)
    )
    assert (
        changed_paths(
            {
                **claude_event("Edit", code / "pipeline/main.nf"),
                "hook_event_name": "PostToolUse",
            },
            code,
        )
        == []
    )
