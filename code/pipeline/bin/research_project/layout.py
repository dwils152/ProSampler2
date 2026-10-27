"""Check this project's directory layout against .agents/layout.toml.

The check is read-only. ``--fix`` only removes regenerable caches and prunes
stale Git worktree metadata; every other finding needs a decision, which the
cleanup-layout skill describes. Run payloads (data, launches, work, results)
and worktrees are never scanned file by file.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import locate_code_root, state_root


CONTRACT = ".agents/layout.toml"
CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
PAYLOAD = (
    "code/data/raw",
    "code/data/store",
    "code/launches",
    "code/work",
    "code/results/runs",
    "code/results/final",
    "code/.worktrees",
)
SKIPPED = {".git", ".pixi", ".nextflow", *CACHES}
RUN_DIRECTORIES = ("launches", "results/runs", "work")
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Problems an agent turn can create; the Stop hook asks the agent to resolve them.
TURN_CHECKS = {"required", "unexpected-entry", "misplaced-file", "adapter", "worktree"}
LISTED = 10


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    message: str
    action: str
    severity: str = "error"


def load_contract(project: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((project / CONTRACT).read_text())
    except FileNotFoundError as error:
        raise ValueError(f"The layout contract {CONTRACT} is missing") from error


def git_files(project: Path) -> tuple[list[str], list[str]] | None:
    """Return tracked and untracked, non-ignored files, or None outside Git."""

    def listed(*options: str) -> list[str] | None:
        completed = subprocess.run(
            ["git", "-C", str(project), "ls-files", "-z", *options],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode:
            return None
        return [path for path in completed.stdout.decode().split("\0") if path]

    tracked = listed("--cached")
    untracked = listed("--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    return tracked, untracked


def walk(project: Path) -> tuple[list[str], list[str]]:
    """Return files and cache directories outside payload and tool directories."""
    payload = {project / path for path in PAYLOAD}
    files: list[str] = []
    caches: list[str] = []
    for directory, subdirectories, names in os.walk(project):
        base = Path(directory)
        caches.extend(
            (base / name).relative_to(project).as_posix()
            for name in subdirectories
            if name in CACHES
        )
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in SKIPPED and base / name not in payload
        )
        files.extend((base / name).relative_to(project).as_posix() for name in names)
    return files, caches


def in_payload(relative: str) -> bool:
    return any(relative == path or relative.startswith(path + "/") for path in PAYLOAD)


def placement_rule(relative: str, contract: dict[str, Any]) -> dict[str, Any] | None:
    name = PurePosixPath(relative).name
    for rule in contract.get("placement", []):
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in rule["patterns"]):
            return rule
    return None


def check_required(project: Path, contract: dict[str, Any]) -> list[Finding]:
    return [
        Finding(
            "required",
            path,
            "Required path is missing",
            f"Restore it (for example `git checkout -- {path}`), or update "
            f"{CONTRACT} if the layout changed on purpose",
        )
        for path in contract.get("required", [])
        if not os.path.lexists(project / path)
    ]


def check_entries(project: Path, contract: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for directory, allowed in contract.get("allowed_entries", {}).items():
        base = project / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for entry in sorted(os.listdir(base)):
            relative = (PurePosixPath(directory) / entry).as_posix()
            if entry in CACHES or entry in allowed:
                continue
            if placement_rule(relative, contract) and (base / entry).is_file():
                continue  # Reported more specifically by check_placement.
            findings.append(
                Finding(
                    "unexpected-entry",
                    relative,
                    f"Not part of the layout of {directory}/",
                    "Move it to a defined location, delete it only with the "
                    f"user's approval, or add it to [allowed_entries] in {CONTRACT}",
                )
            )
    return findings


def check_placement(files: list[str], contract: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in sorted(files):
        if in_payload(relative):
            continue
        rule = placement_rule(relative, contract)
        if rule and not any(relative.startswith(prefix) for prefix in rule["allowed"]):
            findings.append(
                Finding(
                    "misplaced-file",
                    relative,
                    f"{rule['kind']} outside {', '.join(rule['allowed'])}",
                    f"Move it under {rule['allowed'][0]} (keep references working), "
                    "or ask the user where it belongs",
                )
            )
    return findings


def check_tracked_payload(tracked: list[str]) -> list[Finding]:
    return [
        Finding(
            "tracked-payload",
            relative,
            "Generated or raw payload is tracked by Git",
            f"With the user's approval, untrack it with `git rm --cached {relative}` "
            "(the file stays on disk)",
        )
        for relative in sorted(tracked)
        if in_payload(relative)
        and not any(relative == f"{path}/README.md" for path in PAYLOAD)
    ]


def check_adapters(project: Path) -> list[Finding]:
    findings: list[Finding] = []
    claude = project / "CLAUDE.md"
    if claude.is_file() and claude.read_text().splitlines()[:1] != ["@AGENTS.md"]:
        findings.append(
            Finding(
                "adapter",
                "CLAUDE.md",
                "CLAUDE.md must start by importing @AGENTS.md",
                "Restore `@AGENTS.md` as its first line and keep rules in AGENTS.md",
            )
        )
    link = project / ".claude/skills"
    skills = project / ".agents/skills"
    if os.path.lexists(link) and (
        not link.is_symlink() or link.resolve() != skills.resolve()
    ):
        findings.append(
            Finding(
                "adapter",
                ".claude/skills",
                "Must be a symlink to ../.agents/skills",
                "Move any skills into .agents/skills/, then recreate the link with "
                "`ln -s ../.agents/skills .claude/skills`",
            )
        )
    for skill in sorted(skills.iterdir()) if skills.is_dir() else []:
        if not skill.is_dir():
            continue
        definition = skill / "SKILL.md"
        header = (
            re.match(r"^---\n(.*?)\n---\n", definition.read_text(), re.DOTALL)
            if definition.is_file()
            else None
        )
        name = (
            re.search(r"^name:\s*(\S+)\s*$", header[1], re.MULTILINE)
            if header
            else None
        )
        if not name or name[1] != skill.name:
            findings.append(
                Finding(
                    "adapter",
                    f".agents/skills/{skill.name}",
                    "Skill needs a SKILL.md whose front matter name matches its folder",
                    "Fix the SKILL.md front matter or rename the folder",
                )
            )
    return findings


def check_runs(project: Path, state: Path) -> list[Finding]:
    """Compare run directories in the checkout that records runs."""
    # From a worktree, run records live in the main checkout: show full paths.
    prefix = "code" if state.resolve() == (project / "code").resolve() else str(state)
    found: dict[str, set[str]] = {}
    findings: list[Finding] = []
    for directory in RUN_DIRECTORIES:
        found[directory] = set()
        base = state / directory
        for entry in sorted(os.listdir(base)) if base.is_dir() else []:
            if entry == "README.md":
                continue
            if (base / entry).is_dir() and RUN_ID.fullmatch(entry):
                found[directory].add(entry)
            else:
                findings.append(
                    Finding(
                        "run-record",
                        f"{prefix}/{directory}/{entry}",
                        "Not a run directory",
                        "The runner only creates run-ID directories here; ask the "
                        "user before moving or deleting it",
                    )
                )
    orphans = (
        ("launches", "results/runs", "Launch record has no results/runs directory"),
        (
            "results/runs",
            "launches",
            "Run record has no launch record, so `audit` cannot check its source",
        ),
        ("work", "results/runs", "Nextflow working files have no run record"),
    )
    for directory, partner, message in orphans:
        findings.extend(
            Finding(
                "run-record",
                f"{prefix}/{directory}/{run_id}",
                message,
                "Never delete or rename run records. Check `status`/`audit` and ask "
                "the user how to reconcile it",
            )
            for run_id in sorted(found[directory] - found[partner])
        )
    worktrees = state / ".worktrees"
    for checkout in sorted(worktrees.iterdir()) if worktrees.is_dir() else []:
        for directory in RUN_DIRECTORIES:
            base = checkout / "code" / directory
            entries = sorted(os.listdir(base)) if base.is_dir() else []
            findings.extend(
                Finding(
                    "run-record",
                    f"{prefix}/.worktrees/{checkout.name}/code/{directory}/{entry}",
                    "Run recorded inside a worktree instead of the main checkout",
                    "Do not delete it. With the user's approval, move it to the "
                    f"main checkout's {prefix}/{directory}/ if that ID is unused",
                )
                for entry in entries
                if entry != "README.md"
            )
    return findings


def check_worktrees(project: Path, state: Path) -> list[Finding]:
    completed = subprocess.run(
        ["git", "-C", str(project), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        return []
    root = (state / ".worktrees").resolve()
    findings: list[Finding] = []
    registered: set[Path] = set()
    blocks = [block for block in completed.stdout.strip().split("\n\n") if block]
    for index, block in enumerate(blocks):
        lines = block.splitlines()
        path = Path(lines[0].removeprefix("worktree ")).resolve()
        registered.add(path)
        if any(line.startswith("prunable") for line in lines):
            findings.append(
                Finding(
                    "worktree",
                    str(path),
                    "Git still lists a worktree whose directory is gone",
                    "Run `check-layout --fix` to prune the stale entry",
                    "cleanup",
                )
            )
        elif index and path.parent != root:
            findings.append(
                Finding(
                    "worktree",
                    str(path),
                    "Worktree outside code/.worktrees/",
                    "With the user's approval, commit its work, remove it with "
                    "`git worktree remove`, and recreate it with `worktree add NAME`",
                )
            )
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if entry.resolve() not in registered:
            findings.append(
                Finding(
                    "worktree",
                    f"code/.worktrees/{entry.name}",
                    "Not a registered Git worktree",
                    "Inspect it for unsaved work and ask the user before deleting it",
                )
            )
    return findings


def check(project: Path, state: Path) -> list[Finding]:
    """Return all layout findings for a checkout and its run records."""
    contract = load_contract(project)
    files, caches = walk(project)
    listing = git_files(project)
    if listing is not None:
        tracked, untracked = listing
        files = [
            path for path in tracked + untracked if os.path.lexists(project / path)
        ]
    findings = [
        *check_required(project, contract),
        *check_entries(project, contract),
        *check_placement(files, contract),
        *(check_tracked_payload(listing[0]) if listing is not None else []),
        *check_adapters(project),
        *check_runs(project, state),
        *check_worktrees(project, state),
        *(
            Finding(
                "cache",
                path,
                "Regenerable cache",
                "Removed by `check-layout --fix`",
                "cleanup",
            )
            for path in sorted(caches)
        ),
    ]
    return findings


def fix(project: Path, findings: list[Finding]) -> list[str]:
    """Apply only the safe, regenerable cleanups."""
    done: list[str] = []
    for finding in findings:
        path = project / finding.path
        if (
            finding.check == "cache"
            and path.name in CACHES
            and path.is_dir()
            and not path.is_symlink()
        ):
            shutil.rmtree(path)
            done.append(f"Removed cache {finding.path}")
    if any(
        item.check == "worktree" and item.severity == "cleanup" for item in findings
    ):
        subprocess.run(["git", "-C", str(project), "worktree", "prune"], check=True)
        done.append("Pruned stale worktree metadata")
    return done


def describe(findings: list[Finding], limit: int | None = None) -> str:
    shown = findings if limit is None else findings[:limit]
    lines = [
        f"- [{item.check}] {item.path}: {item.message}. {item.action}."
        for item in shown
    ]
    if limit is not None and len(findings) > limit:
        lines.append(f"- ... and {len(findings) - limit} more (run `check-layout`)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print findings as JSON")
    parser.add_argument(
        "--fix", action="store_true", help="Remove caches; prune worktrees"
    )
    parser.add_argument("--hook", choices=["start", "stop"], help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.hook == "stop":
        try:
            event = json.load(sys.stdin)
        except ValueError:
            event = {}
        # Ask once per stop; a second block could keep the agent looping.
        if isinstance(event, dict) and event.get("stop_hook_active"):
            return 0
    try:
        code = locate_code_root()
        project, state = code.parent, state_root(code)
        findings = check(project, state)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if args.hook:
            # A broken check must never block or derail an agent session.
            print(json.dumps({"systemMessage": f"Layout check skipped: {error}"}))
            return 0
        print(f"Layout check failed: {error}", file=sys.stderr)
        return 1
    errors = [item for item in findings if item.severity == "error"]

    if args.hook == "start":
        if errors:
            print(
                f"Layout check: {len(errors)} problem(s) against {CONTRACT}. Use the "
                "cleanup-layout skill before structural work, and never delete or "
                "move user files, data or run records without approval.\n"
                + describe(errors, LISTED)
            )
        return 0
    if args.hook == "stop":
        blocking = [item for item in errors if item.check in TURN_CHECKS]
        if not blocking:
            return 0
        print(
            f"The project layout no longer matches {CONTRACT}:\n"
            + describe(blocking, LISTED)
            + "\nResolve these with the cleanup-layout skill before finishing: move "
            "files you created to their defined places, and ask the user before "
            "deleting anything or adding an exception to the contract. If they "
            "cannot be resolved now, say so to the user.",
            file=sys.stderr,
        )
        return 2

    if args.fix:
        for line in fix(project, findings):
            print(line)
        findings = check(project, state)
        errors = [item for item in findings if item.severity == "error"]
    if args.json:
        print(json.dumps([asdict(item) for item in findings], indent=2))
    elif findings:
        cleanup = len(findings) - len(errors)
        print(
            f"Layout check against {CONTRACT}: {len(errors)} problem(s), "
            f"{cleanup} cleanup item(s)\n" + describe(findings)
        )
    else:
        print(f"Layout matches {CONTRACT}.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
