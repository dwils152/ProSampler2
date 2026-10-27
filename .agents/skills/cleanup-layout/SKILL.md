---
name: cleanup-layout
description: Bring the project directory back in line with its layout contract (.agents/layout.toml) - stray or misplaced files, missing contract paths, broken agent adapters, tracked payloads, orphaned run records, worktrees outside code/.worktrees and caches. Use when check-layout or a layout hook reports problems, after creating files in new places, or when asked to tidy or clean up the project structure.
---

Run `check-layout` through the root README's Pixi prefix (`--json` gives
machine-readable findings). The check is read-only. `check-layout --fix` applies
only the safe cleanups: it removes `__pycache__`, `.pytest_cache` and
`.ruff_cache` directories and prunes stale Git worktree entries.

Resolve every other finding by moving things to where the layout defines them,
never by loosening the contract or deleting evidence:

- **misplaced-file / unexpected-entry you created:** move it (`git mv` when
  tracked) and update references. Task code and tests belong under
  `code/pipeline/bin/research_project/` and `tests/`; processes, subworkflows and
  configuration under `code/pipeline/modules/`, `subworkflows/` and `conf/`; raw
  inputs under `code/data/raw/` and reusable derived data under `code/data/store/`;
  project notes and decisions under `code/docs/`; publication material under
  `manuscript/`; procedures under `.agents/skills/`.
- **Files you did not create:** list them and ask the user whether to move,
  delete or keep them. Delete only with explicit approval.
- **required:** restore an accidental deletion from Git (`git checkout -- PATH`);
  otherwise ask.
- **adapter:** restore `@AGENTS.md` as the first line of `CLAUDE.md` and the
  `.claude/skills -> ../.agents/skills` symlink. Move agent-specific rules or
  skills into `AGENTS.md` or `.agents/skills/`.
- **tracked-payload:** propose `git rm --cached PATH` (the file stays on disk)
  and apply it only with approval.
- **run-record:** never delete, rename or edit run records. Report the orphan,
  inspect it with `status` and `audit`, and ask the user how to reconcile it.
  Records found inside a worktree may be moved to the main checkout's matching
  directory only with approval and when that run ID is unused there.
- **worktree:** a worktree outside `code/.worktrees/` should have its work
  committed, be removed with `git worktree remove` and be recreated with
  `worktree add NAME`, with the user's approval. An unregistered directory in
  `code/.worktrees/` may hold unsaved work: inspect it and ask before deleting.

When the user wants a new structure, change `.agents/layout.toml` and the README
layout together, in the same change, and say so in the report.

Rerun `check-layout` and report what was moved, what remains and which
decisions the user still needs to make. Raw data stays immutable, and nothing
is stashed, reset or discarded to make the check pass.
