"""Agent hook that checkpoints the previous run before experiment design edits."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .agent_events import edited_paths
from .git_checkpoint import GitCheckpointError, require_previous_experiment_checkpoint
from .paths import locate_code_root, state_root


DESIGN_PATHS = (
    "pipeline/conf/",
    "pipeline/modules/",
    "pipeline/subworkflows/",
    "pipeline/bin/research_project/",
)
DESIGN_FILES = {"pipeline/main.nf", "pipeline/nextflow.config"}


def changed_paths(event: dict[str, Any], code: Path) -> list[Path]:
    """Return contained code-relative paths named by a pending edit."""
    code = code.resolve()
    selected: set[Path] = set()
    for candidate in edited_paths(event, "PreToolUse", code.parent):
        try:
            selected.add(candidate.relative_to(code))
        except ValueError:
            continue
    return sorted(selected)


def affects_experiment_design(paths: list[Path]) -> bool:
    for path in paths:
        relative = path.as_posix()
        if relative in DESIGN_FILES or any(
            relative.startswith(item) for item in DESIGN_PATHS
        ):
            return True
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Hook input must be a JSON object")
        code = locate_code_root()
        if not affects_experiment_design(changed_paths(event, code)):
            return 0
        checkpoint = require_previous_experiment_checkpoint(state_root(code))
        if checkpoint["status"] == "created":
            print(
                json.dumps(
                    {
                        "systemMessage": (
                            f"Committed previous experiment {checkpoint['run_id']} "
                            f"to {checkpoint['branch']} at {checkpoint['commit']}."
                        )
                    }
                )
            )
        return 0
    except (OSError, ValueError, json.JSONDecodeError, GitCheckpointError) as error:
        print(f"Experiment design blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
