---
name: audit-evidence
description: Review a completed experiment or scientific claim against its recorded configuration, source, outputs and limitations.
---

Start with the run record under `code/results/runs/` and its source snapshot
under `code/launches/`. Use `audit --run-id ID` through Pixi for integrity checks;
then assess the domain-specific evidence. This starter's content audit covers
only the random-number example and must be extended for new workflows.

Trace each claim to the invoked workflow and its expected terminal outputs.
Check seeds, splits, biological units, exclusions, failures and missing cases.
State incompatible comparisons and any use of held-out results for selection.
An execution success or integrity audit is not itself scientific validation.

Report supported conclusions, limits and provenance gaps. Keep raw data and
recorded results intact; promotion to final results requires explicit scope.
