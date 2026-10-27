---
name: handoff-session
description: Save or resume an experiment session using a concrete handoff tied to its run record, particularly before interruption or context loss.
---

Use `handoff --run-id ID --objective TEXT --next-step TEXT --notes TEXT` through
Pixi. Include completed validation, unresolved issues, and any Slurm job IDs in
the notes. The command records the current terminal state and evidence paths.
Update the note at meaningful milestones; do not rely solely on session exit.

On resumption, use `latest-handoff` or read the requested run's `handoff.md`.
Refresh `status`, relevant artifacts, and live scheduler status before acting.
Keep the distinction between completed checks, proposed work and unknowns.
An interrupted driver can leave scheduler jobs active. Never duplicate them.

For work with no experiment run, use `code/docs/session.md` as a short note
containing the objective, touched files, completed checks and next action.
Do not create a dummy scientific run merely to record coding progress.
