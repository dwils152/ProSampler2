"""Agent hook that asks for a Git commit before launching on a dirty tree."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .agent_events import shell_command


# Prefixing a launch with this assignment records that the user declined to commit.
ACKNOWLEDGEMENT = "ALLOW_DIRTY_LAUNCH=1"
LISTED_CHANGES = 20
OPERATORS = ";&|\n"
PIXI_VALUE_OPTIONS = {"--manifest-path", "-e", "--environment", "--color"}
SHELLS = {"bash", "sh", "zsh"}
PROJECT = Path(__file__).resolve().parents[4]


def split_commands(command: str) -> list[list[str]]:
    """Split shell text into simple commands, respecting quotes."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [command.split()]

    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and all(character in OPERATORS for character in token):
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def pixi_task(arguments: list[str]) -> str | None:
    """Return the task or command named after ``pixi run`` options."""
    skip = False
    for argument in arguments:
        if skip:
            skip = False
        elif argument in PIXI_VALUE_OPTIONS:
            skip = True
        elif not argument.startswith("-"):
            return argument
    return None


def experiment_launches(command: str) -> list[bool]:
    """Return one acknowledgement flag per experiment launch in shell text."""
    launches: list[bool] = []
    for words in split_commands(command):
        for index, word in enumerate(words):
            rest = words[index + 1 :]
            acknowledged = ACKNOWLEDGEMENT in words[:index]
            if (
                Path(word).name in SHELLS
                and len(rest) > 1
                and re.fullmatch(r"-[a-z]*c[a-z]*", rest[0])
            ):
                launches.extend(
                    flag or acknowledged for flag in experiment_launches(rest[1])
                )
                break
            if (
                Path(word).name == "pixi"
                and rest[:1] == ["run"]
                and pixi_task(rest[1:]) == "experiment"
            ) or (word == "-m" and rest[:2] == ["research_project.runner", "run"]):
                launches.append(acknowledged)
                break
    return launches


def uncommitted_changes(project: Path) -> list[str] | None:
    """Return porcelain status lines, or None when Git state is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    return [line for line in completed.stdout.splitlines() if line]


def pause_message(changes: list[str]) -> str:
    listed = changes[:LISTED_CHANGES]
    remaining = len(changes) - len(listed)
    return "\n".join(
        [
            "Experiment launch paused: the Git working tree has uncommitted changes.",
            *(f"  {line}" for line in listed),
            *([f"  ... and {remaining} more"] if remaining else []),
            "Ask the user whether to commit these changes before launching, and "
            "propose a commit message. Commit only with their approval; never "
            "stash, reset or discard changes. If the user declines, rerun the same "
            f"command prefixed with {ACKNOWLEDGEMENT}. The run manifest records "
            "the dirty state.",
        ]
    )


def check(event: dict[str, Any], project: Path) -> str | None:
    """Return a pause message when an unacknowledged launch meets a dirty tree."""
    command = shell_command(event, "PreToolUse")
    if command is None:
        return None
    launches = experiment_launches(command)
    if not launches or all(launches):
        return None
    changes = uncommitted_changes(project)
    return pause_message(changes) if changes else None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Hook input must be a JSON object")
    except ValueError as error:
        # This hook sees every shell command; its own failure must not block them.
        print(json.dumps({"systemMessage": f"Launch check skipped: {error}"}))
        return 0

    message = check(event, PROJECT)
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
