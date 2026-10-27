"""Format Python files named by a completed agent edit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .agent_events import edited_paths
from .runner import locate_code_root


def python_paths(event: dict[str, Any], root: Path) -> list[Path]:
    """Return existing, regular Python files contained by this project."""
    root = root.resolve()
    selected: list[Path] = []
    for candidate in edited_paths(event, "PostToolUse", root):
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.suffix == ".py" and candidate.is_file():
            selected.append(candidate)
    return selected


def report(message: str) -> None:
    """Return a visible, non-blocking hook message to the agent."""
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Hook input must be a JSON object")

        root = locate_code_root().parent
        paths = python_paths(event, root)
        if not paths:
            return 0

        relative = [str(path.relative_to(root)) for path in paths]
        completed = subprocess.run(
            ["ruff", "format", *relative],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            report(f"Automatic Python formatting failed: {detail[-1500:]}")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report(f"Automatic Python formatting failed: {error}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
