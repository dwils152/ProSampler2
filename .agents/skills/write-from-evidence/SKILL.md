---
name: write-from-evidence
description: Draft or revise manuscript claims, results, abstracts, captions, and source-data notes from this project's audited run records. Use when scientific writing must trace statements and numbers to recorded evidence; do not use to run experiments or promote results.
---

Write only from evidence that can be traced to this project's run records. Match
the user's requested document, section and level of interpretation; do not expand
the manuscript scope merely because more results are available.

## Establish the evidence set

Identify the intended run IDs before making scientific claims. If the user asks
for the latest result, use `catalog --refresh runs` once and report any remaining
pending records. If no run is specified, inspect likely candidates and ask only
when choosing among them would change the conclusion.

For every selected run, use the documented Pixi prefix and run:

```text
pixi run --locked --manifest-path code/pixi.toml audit --run-id ID
pixi run --locked --manifest-path code/pixi.toml catalog evidence --run-id ID
```

Treat the catalog as a discovery and query layer. Treat the run record and fresh
audit as the integrity evidence. Inspect `manifest.json`, `audit.json`,
`terminal.json`, `resolved-config.json`, and the named terminal artifacts when a
claim depends on details not represented by catalog metrics.

Do not write an affirmative result from a failed or missing audit, an
unsuccessful run, a null metric, an artifact hash mismatch, or an unresolved
provenance gap. Describe the gap and omit the affected conclusion. Never alter a
run record to make it support a claim.

## Check comparisons and inference

For writing that compares runs, call `catalog compare` for the selected run IDs
and verify dataset ID, split ID, biological unit, evaluation ID, metric unit and
metric definition. State mismatches. Do not rank, pool or describe one run as
better when those fields are incompatible or missing.

Check recorded seeds, exclusions, failed and missing cases, selection rules and
notes. Do not use held-out or test results to choose models, thresholds or
analysis definitions. Label an analysis as exploratory when its predefinition
cannot be established from the record.

Separate what was observed from what it may imply. Internal predictive metrics
alone do not establish biological validity. Do not invent uncertainty,
statistical significance, generalization, causality, mechanism, clinical value
or biological importance when the recorded methods and outputs do not establish
them.

## Write the requested material

Preserve the manuscript's terminology, structure and citation style. Use the
precision supported by the artifact and include units and metric definitions.
Keep observed results, interpretation and limitations distinguishable in the
prose. Use run IDs as stable provenance identifiers and retain the exact source
path for every quantitative or comparative claim.

Edit files under `manuscript/` only when the user requested a file change.
It is a separate Git repository (a submodule): commit there, not in the
project, and only when asked. Otherwise return a draft in the response. Do not copy artifacts into
`code/results/final/`; promotion requires a separate explicit request.

After drafting, review every number, comparison and scientific conclusion
against its evidence. Report a concise evidence ledger with:

- claim or sentence identifier;
- supporting run ID or IDs;
- metric or artifact source path;
- audit status and recorded hash when available;
- material limitation or provenance gap.

Report the audit and catalog commands used, their results, and any scope that
was not checked. If external literature is needed for background or broader
interpretation, distinguish those citations from project-generated evidence.
