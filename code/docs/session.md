# Project handoff

Objective — Add a minimal-maintenance DuckDB/Parquet research catalog with fast
queries, independent concurrent experiment writes and recoverable indexing.

Run ID — `smoke-20260926-055052-1fd78e31`. The local workflow completed and its
output audit and catalog integration check passed. Its
[run handoff](../results/runs/smoke-20260926-055052-1fd78e31/handoff.md) is the
authoritative validation handoff. Generated evidence is local and ignored by Git.

Completed — DuckDB catalog snapshots and a durable request queue; automatic
indexing after experiments, handoffs and notes; JSON query/compare/evidence/search
commands; versioned notes; Parquet publication and audit; native metric contracts;
resource bounds; dependency lock; documentation and 61 passing tests. Pipeline,
Pixi manifest and lockfile match the passing run's source inventory.

Performance — On 1,000 synthetic run records, API queries took 18.667 ms to list
100 runs and 15.234 ms to aggregate metrics, excluding process startup. Catalog
construction took 15.106 seconds.

Preserved failure — `smoke-20260926-054802-4ff3351f` stopped before task execution
on unavailable Git metadata. The Git check and a filesystem-specific exclusive
lock issue were corrected and the smoke run was retried with a fresh ID.

Unchecked — Cross-node Slurm/shared-filesystem behavior, large-data
performance and semantic embeddings. No scheduler jobs were submitted; the local
driver inherited the existing allocation `27032585`.

Next action — Use `catalog runs` to inspect the scaffold, then define the first
scientific experiment's immutable dataset/split identifiers, biological units,
metric definitions and acceptance criteria in native Nextflow configuration.
Refresh live status and audit evidence before resuming experiment work.
