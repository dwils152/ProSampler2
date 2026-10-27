"""Create and remove this project's Git worktrees under code/.worktrees/.

Worktrees hold source only. Runs launched from them are recorded in the main
checkout (see ``runner.state_root``), so removing a worktree loses no evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import locate_code_root, state_root


DIRECTORY = ".worktrees"
BRANCH_PREFIX = "worktree/"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def worktree_root(code: Path) -> Path:
    """Return code/.worktrees/ of the main checkout, from any checkout."""
    return state_root(code) / DIRECTORY


def slug(name: str) -> str:
    """Turn a requested name into a safe directory and branch component."""
    value = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-.")[:64]
    if not NAME.fullmatch(value):
        raise ValueError(f"Unusable worktree name: {name!r}")
    return value


def git(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValueError(detail or f"git {' '.join(arguments)} failed")
    return completed


def install_environment(path: Path) -> None:
    """Install the locked Pixi environment so the worktree runs its own source."""
    pixi = os.environ.get("PIXI_EXE") or shutil.which("pixi")
    if pixi is None:
        print(
            "Pixi not found; run `pixi install --locked` in the worktree.",
            file=sys.stderr,
        )
        return
    # Drop the calling environment's activation so Pixi targets the new manifest.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIXI_", "CONDA_"))
    }
    subprocess.run(
        [pixi, "install", "--locked", "--manifest-path", str(path / "code/pixi.toml")],
        stdout=sys.stderr,
        stderr=sys.stderr,
        env=environment,
        check=True,
    )


def add(
    code: Path,
    name: str,
    *,
    branch: str | None = None,
    base: str | None = None,
    install: bool = True,
) -> Path:
    """Create code/.worktrees/<name> on a new or existing branch."""
    name = slug(name)
    path = worktree_root(code) / name
    if path.exists():
        raise ValueError(f"Worktree already exists: {path}")
    branch = branch or f"{BRANCH_PREFIX}{name}"
    checkout = code.parent
    exists = (
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
        ).returncode
        == 0
    )
    if exists:
        if base:
            raise ValueError(f"Branch {branch} already exists; omit --base to use it")
        arguments = ["worktree", "add", str(path), branch]
    else:
        arguments = [
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            *([base] if base else []),
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    print(git(checkout, *arguments).stderr.strip(), file=sys.stderr)
    if install:
        install_environment(path)
    return path


def remove(code: Path, target: str | Path) -> Path:
    """Remove a clean worktree under code/.worktrees/ and keep its branch."""
    root = worktree_root(code).resolve()
    candidate = Path(target)
    path = (
        candidate if candidate.is_absolute() else root / slug(str(target))
    ).resolve()
    if path.parent != root:
        raise ValueError(f"Only worktrees directly under {root} are managed here")
    try:
        git(root.parent.parent, "worktree", "remove", str(path))
    except ValueError as error:
        if "containing submodules" not in str(error):
            raise
        # Its submodule Git data lives inside the worktree's metadata, so a
        # forced removal would also delete manuscript commits made only here.
        raise ValueError(
            f"Git will not remove {path.name} because a submodule (such as "
            "manuscript/) was initialized in it. Push or otherwise keep any "
            "commits made in that submodule, then, with the user's agreement, run "
            f"`git worktree remove --force {path}`"
        ) from error
    return path


def listed(code: Path) -> list[dict[str, str]]:
    """Return the worktrees under code/.worktrees/ with their branches."""
    root = worktree_root(code).resolve()
    output = git(code.parent, "worktree", "list", "--porcelain").stdout
    entries: list[dict[str, str]] = []
    for block in output.strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path = Path(fields.get("worktree", ""))
        if path.parent == root:
            branch = fields.get("branch", "(detached)").removeprefix("refs/heads/")
            entries.append({"name": path.name, "path": str(path), "branch": branch})
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    creation = commands.add_parser("add", help="Create code/.worktrees/NAME")
    creation.add_argument("name")
    creation.add_argument("--branch", help=f"Default: {BRANCH_PREFIX}NAME")
    creation.add_argument("--base", help="Start a new branch here (default: HEAD)")
    creation.add_argument("--no-install", action="store_true")
    removal = commands.add_parser(
        "remove", help="Remove a clean worktree; keep its branch"
    )
    removal.add_argument("name")
    commands.add_parser("list")
    # Claude Code WorktreeCreate/WorktreeRemove hooks send JSON on stdin.
    commands.add_parser("hook-create")
    commands.add_parser("hook-remove")
    args = parser.parse_args()
    try:
        code = locate_code_root()
        if args.action == "add":
            print(
                add(
                    code,
                    args.name,
                    branch=args.branch,
                    base=args.base,
                    install=not args.no_install,
                )
            )
        elif args.action == "remove":
            print(f"Removed {remove(code, args.name)}")
        elif args.action == "list":
            print(json.dumps(listed(code), indent=2))
        elif args.action == "hook-create":
            # Claude Code reads the new worktree's absolute path from stdout.
            print(add(code, str(json.load(sys.stdin)["name"])))
        else:
            remove(code, Path(json.load(sys.stdin)["worktree_path"]))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Worktree error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
