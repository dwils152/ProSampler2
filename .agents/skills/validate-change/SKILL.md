---
name: validate-change
description: Validate changes to this project's Python helpers, Nextflow processes, configurations or orchestration before handoff.
---

Inspect the diff and identify the affected behavior. Run the smallest relevant
tests through Pixi. For orchestration changes, run `smoke`; it executes the
example process graph and audits its outputs. Use `verify` for the complete
starter checks. All tasks and the command prefix are in the root README.

When adding a workflow, add generated fixtures and an audit of its terminal
outputs. Keep acceptance criteria independent of held-out scientific scores.
Check behavior rather than only source spelling or aggregate test coverage.

Report each command's exit status, relevant artifacts and scope not checked.
Reuse successful checks unless subsequent changes invalidate them. A graph
preview is not a substitute for executing a smoke workflow.
