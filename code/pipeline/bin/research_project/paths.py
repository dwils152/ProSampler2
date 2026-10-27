"""Locate this checkout's code directory and where its runs are recorded.

Kept free of heavy imports so agent hooks can use it on every turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def locate_code_root() -> Path:
    candidates = (Path.cwd(), Path.cwd() / "code", Path(__file__).resolve().parents[3])
    for candidate in candidates:
        if (candidate / "pixi.toml").is_file() and (
            candidate / "pipeline/bin/research_project"
        ).is_dir():
            return candidate.resolve()
    raise ValueError(
        "Run from the project root or code/ using the documented Pixi tasks"
    )


def state_root(code: Path) -> Path:
    """Return the code directory that holds run records for this checkout.

    A linked Git worktree runs its own source but records launches, working
    files, results, data and the catalog in the main checkout, so run IDs and
    evidence stay in one place and survive removal of the worktree.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(code),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return code
    lines = completed.stdout.splitlines()
    if completed.returncode or len(lines) != 3:
        return code
    top, git_dir, common = (Path(line).resolve() for line in lines)
    if git_dir == common:
        return code
    try:
        main = common.parent / code.resolve().relative_to(top)
    except ValueError:
        return code
    if not (main / "pixi.toml").is_file():
        raise ValueError(
            f"This worktree records runs in the main checkout, but {main} is missing"
        )
    return main
