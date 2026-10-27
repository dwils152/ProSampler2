from pathlib import Path

from research_project import format_hook


def event(command: str) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": command},
    }


def claude_event(tool_name: str, file_path: Path) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)},
    }


def test_extracts_only_existing_python_files_inside_project(tmp_path: Path) -> None:
    source = tmp_path / "src/example.py"
    source.parent.mkdir()
    source.write_text("x=1\n")
    text = "\n".join(
        [
            f"*** Add File: {source}",
            f"*** Update File: {tmp_path / 'missing.py'}",
            f"*** Add File: {tmp_path / 'notes.md'}",
            "*** Add File: /tmp/outside.py",
        ]
    )

    assert format_hook.python_paths(event(text), tmp_path) == [source]


def test_ignores_other_tools_and_malformed_input(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x=1\n")

    assert (
        format_hook.python_paths(
            {**event(f"*** Add File: {source}"), "tool_name": "Bash"}, tmp_path
        )
        == []
    )
    assert (
        format_hook.python_paths(
            {"hook_event_name": "PostToolUse", "tool_name": "apply_patch"}, tmp_path
        )
        == []
    )


def test_extracts_claude_code_edits_inside_project(tmp_path: Path) -> None:
    source = tmp_path / "src/example.py"
    source.parent.mkdir()
    source.write_text("x=1\n")
    notes = tmp_path / "notes.md"
    notes.write_text("notes\n")

    for tool_name in ("Edit", "MultiEdit", "Write"):
        assert format_hook.python_paths(claude_event(tool_name, source), tmp_path) == [
            source
        ]
    assert format_hook.python_paths(claude_event("Write", notes), tmp_path) == []
    assert (
        format_hook.python_paths(
            claude_event("Edit", Path("/tmp/outside.py")), tmp_path
        )
        == []
    )
    assert (
        format_hook.python_paths(
            {**claude_event("Edit", source), "hook_event_name": "PreToolUse"}, tmp_path
        )
        == []
    )


def test_relative_patch_paths_follow_the_agent_working_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "code/pipeline/bin/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("x=1\n")
    nested = {**event("*** Update File: example.py"), "cwd": str(source.parent)}

    assert format_hook.python_paths(nested, tmp_path) == [source]
