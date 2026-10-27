# Working in this project

- Inspect the working tree before editing. Preserve unrelated changes. Use one
  writer per worktree unless the user requests concurrent work.
- Create Git worktrees only under `code/.worktrees/`, with the `worktree add
  NAME` task (it also installs the worktree's environment). Runs launched from
  any worktree are recorded in the main checkout's `code/launches/`,
  `code/results/runs/`, `code/work/`, `code/data/` and catalog. Read raw data
  through `params.data_dir`, not paths relative to the checkout.
- Use Pixi and `NXF_SYNTAX_PARSER=v2`. Run commands from the project root with
  `pixi run --locked --manifest-path code/pixi.toml TASK`.
- Keep scientific parameters and execution settings in `code/pipeline/conf/`.
  Generated run-specific values belong in `code/launches/<run_id>/`.
- Keep orchestration in Nextflow, task implementation and tests under
  `code/pipeline/bin/`, and publication material under `manuscript/`.
- `manuscript/` is a separate Git repository checked out as a submodule. Commit
  manuscript changes inside it, then record its new commit in the parent, each
  only with approval. Manuscript changes count toward a dirty tree before a
  launch. Leave it uninitialized in worktrees and edit it in the main checkout.
- Keep the directory structure consistent with `.agents/layout.toml`. Run
  `check-layout` after creating files in new places and before a handoff, and
  resolve findings with the cleanup-layout skill. Change the contract only for
  an intended structural change, together with the README layout.
- Raw data is immutable. Reusable derived checkpoints belong in `code/data/store/`.
  Never manually modify `code/work/` or Git internals. Promotion into
  `code/results/final/` requires an explicit promotion request.
- Use one run ID across launches, working state and results. Preserve failed
  runs, missing cases, source/config hashes, seeds, splits and biological units.
- Before launching an experiment, check `git status`. If the tree is dirty, list
  the changes, propose a commit message and ask the user whether to commit.
  Commit only with approval. If they decline, prefix the launch command with
  `ALLOW_DIRTY_LAUNCH=1`. Never stash, reset or discard changes to get a clean tree.
- Do not select models, thresholds or analysis definitions using held-out/test
  results. Do not change labels or evaluation definitions to improve results.
- State mismatched splits, provenance gaps and limits of scientific inference.
  Internal predictive metrics alone do not establish biological validity.
- Validate changed logic with relevant tests. After orchestration changes, run
  a bounded Nextflow smoke workflow. Report commands, results and unchecked scope.
- Stop on insufficient provenance, destructive actions outside authorization,
  an already-active intended session, unbounded work, or recurrence of the same
  failure after a reasoned correction. Preserve evidence for the next decision.
- Before a session handoff, record the objective, run ID, verified status,
  completed checks, unresolved issues and next action in the run record. On
  resumption, refresh live status before acting.

Use the relevant procedures in `.agents/skills/`. Skills do not expand the
user's authorization. Routine authorized work should proceed without repeated
confirmation. Do not install external services or launch production experiments
merely because a template example mentions them.

This file and `.agents/skills/` are shared by every coding agent. Put rules and
procedures here, and hook behavior in Pixi tasks. Keep agent-specific files
(`CLAUDE.md`, `.claude/`, `.codex/`) as thin adapters that point back here.
