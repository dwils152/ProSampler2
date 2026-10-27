"""Read the files named by Codex and Claude Code file-edit hook events."""

from __future__ import annotations

from pathlib import Path
from typing import Any


# Codex edits through one patch tool; Claude Code uses a tool per edit style.
PATCH_TOOL = "apply_patch"
PATCH_PREFIXES = ("*** Add File: ", "*** Update File: ", "*** Move to: ")
FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
FILE_FIELDS = ("file_path", "notebook_path")
# Both agents report shell commands to hooks under the same tool name.
SHELL_TOOL = "Bash"


def shell_command(event: dict[str, Any], hook_event: str) -> str | None:
    """Return the shell command text for one hook event, if any.

    An argument vector is returned one argument per line so that a wrapped
    script such as ``bash -lc SCRIPT`` remains visible as its own line.
    """
    if (
        event.get("hook_event_name") != hook_event
        or event.get("tool_name") != SHELL_TOOL
    ):
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return "\n".join(command)
    return None


def edited_paths(event: dict[str, Any], hook_event: str, project: Path) -> list[Path]:
    """Return resolved paths named by a supported edit tool for one hook event.

    Relative paths resolve against the agent's reported working directory,
    falling back to the project root. Callers apply their own containment rules.
    """
    if event.get("hook_event_name") != hook_event:
        return []
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return []

    supplied: list[str] = []
    tool_name = event.get("tool_name")
    if tool_name == PATCH_TOOL and isinstance(tool_input.get("command"), str):
        for line in tool_input["command"].splitlines():
            prefix = next(
                (value for value in PATCH_PREFIXES if line.startswith(value)), None
            )
            if prefix is not None:
                supplied.append(line.removeprefix(prefix).strip())
    elif tool_name in FILE_TOOLS:
        supplied.extend(
            tool_input[field]
            for field in FILE_FIELDS
            if isinstance(tool_input.get(field), str)
        )

    base = Path(event["cwd"]) if isinstance(event.get("cwd"), str) else project
    return sorted(
        {
            (path if path.is_absolute() else base / path).resolve()
            for path in map(Path, supplied)
            if str(path)
        }
    )
