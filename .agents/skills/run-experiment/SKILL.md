---
name: run-experiment
description: Prepare and execute an authorized experiment from this project's native Nextflow configurations, preserving its run record and execution bounds.
---

Confirm the requested configuration, execution profile and scope from the
conversation. Scientific settings belong in `code/pipeline/conf/`. Inspect the
configuration's inputs, output contract and finite driver/task limits before
launch. On HPC, check the intended session and job IDs before submitting work.
If `git status` shows uncommitted changes, ask the user whether to commit them
first, as `AGENTS.md` describes; the optional `hook-pre-shell` hook enforces this.

Use `experiment --run-id ID --config NAME.config --profile local|slurm` through
the root README's Pixi command prefix. The runner freezes code and config,
refuses existing IDs, runs Nextflow, audits outputs and writes a handoff.
Use `status --run-id ID` and `audit --run-id ID` to inspect recorded evidence.

A failed preparation still reserves its ID. Keep that record. Investigate a
failure once, apply a justified correction and use a new ID only within the
existing authorization. Stop if the same failure recurs. Resume a conversation
from its handoff; do not restart an existing Nextflow session blindly.
