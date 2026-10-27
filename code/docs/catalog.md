# Research catalog

DuckDB stores run metadata, explicit summary metrics, artifact references and
research notes. Larger tabular outputs stay in Parquet. The catalog can be
recreated from run records; it is not the sole copy of research evidence.
No service, credentials, model download or extension installation is required.

## Commands

Run from the project root with
`pixi run --locked --manifest-path code/pixi.toml catalog` followed by:

| Arguments | Result |
| --- | --- |
| `runs --limit 100` | Recorded runs, newest first |
| `runs --state failed` | Filter by recorded execution state |
| `runs --dataset-id DATASET` | Filter by the exact dataset identifier |
| `evidence --run-id ID` | Metadata, metrics, artifact hashes, missing cases and notes |
| `compare --run-id A --run-id B` | Values side by side, with provenance and definition mismatches |
| `query --sql "SELECT name, avg(value) FROM metrics GROUP BY name"` | One bounded SELECT against catalog tables |
| `parquet --run-id ID --path example/samples.parquet --sql "SELECT avg(value) FROM result"` | SELECT against a registered Parquet artifact |
| `search --query "seed sensitivity"` | Notes containing all terms, ignoring case |
| `search --query "seed" --history` | Include superseded note revisions |
| `note --run-id ID --kind decision --author researcher --text "..."` | Append a research note and request indexing |
| `note ... --supersedes NOTE_ID` | Revise a note while retaining its original record |
| `status` | Catalog generation, time, failures and pending requests |
| `refresh` | Process one bounded batch of pending requests |
| `refresh --rebuild` | Recreate a complete snapshot from the run directories |

Put `--refresh` before a query subcommand to attempt an update before reading:
`catalog --refresh runs`. Without this option, queries do not ingest records,
rehash artifacts or scan experiment directories. A failed refresh is reported
alongside the previous snapshot when one exists. `refresh` exits nonzero for
pending failures or a busy indexer. `_refresh` is an internal worker command.

The corresponding Python functions are `list_runs`, `compare_runs`,
`get_evidence`, `search_notes`, `select_query` and `read_parquet` in
`research_project.catalog`. Each accepts the project's `code/` directory.
These provide a small interface an agent can call without constructing SQL.

## Data contract

The indexer reads `manifest.json`, `terminal.json`, `audit.json`, explicitly
declared metric JSON files, and versioned notes in each run directory. Legacy
`handoff.md` files remain searchable. Updating a legacy handoff archives its
previous text before replacement. The current handoff remains at `handoff.md`;
`handoff.json` points to its revision under `notes/`.

Declare scientific identifiers and metric definitions in the selected native
Nextflow configuration, as demonstrated in `pipeline/conf/example.config`:

```groovy
params {
    dataset_id = 'dataset-version-or-content-id'
    split_id = 'split-membership-version-or-hash'
    biological_unit = 'specimen'
    evaluation_id = 'predeclared-evaluation-v1'
    catalog_metrics = [
        [name: 'mean', path: 'analysis/summary.json', key: 'mean',
         unit: 'dimensionless', definition_id: 'arithmetic-mean-v1']
    ]
}
```

This is a contract example, not an instruction to change a running analysis.
The metric source must also appear in `expected_outputs`. Metric keys refer to
top-level finite numeric values. Missing values remain null with an explanation.
Values whose source differs from its recorded audit hash are withheld. Other
large artifact hashes are copied from the recorded audit, not recomputed during
indexing. A fresh `audit --run-id ID` remains the verification operation.

The synthetic example's identifiers describe its generator and explicitly mark
splits and biological units as inapplicable. A scientific adaptation must record
real immutable input versions/hashes, split membership, seeds and biological
units in its run evidence. Missing identifiers are not guessed.

| Table | Contents |
| --- | --- |
| `runs` | Run ID, execution/audit state, timestamps, seed, comparison identifiers, source/configuration digests, parameters, provenance gaps |
| `artifacts` | Relative path, format, expected/present flags, size and recorded audit hash |
| `metrics` | Run ID, metric name, value, unit, definition ID, source path/hash and missing-value reason |
| `notes` | Versioned text, kind, author, timestamp, source path/hash, superseded note and current flag |

Paths in tables are relative to their run directory except `runs.manifest_path`,
which is relative to `code/`. `evidence` supplies the absolute record directory.
Typed catalog tables make repeated summary queries inexpensive. Parquet queries
scan only the requested registered artifact through a view named `result`.
The SQL interface allows one SELECT, limits returned rows, disables arbitrary
external access, and applies a query timeout and memory/thread bounds.

`compare` reports missing runs/metrics, mismatched datasets, splits, biological
units, evaluation definitions and metric units/definitions. It retains failed
runs and provenance gaps. It does not rank models, pool biological units, select
thresholds or establish scientific validity. Even matching recorded definitions
do not prove datasets or analyses are scientifically comparable. Full parameters
and source references are retained for review.

## Concurrent experiments and recovery

Each runner saves its own evidence before requesting an update. Requests are
individual JSON files with unique IDs under `code/data/store/catalog/queue/`.
They are published by atomic rename after flushing their contents. A busy or
failed catalog never changes the scientific process outcome. The run's
`catalog-index.json` records its last automatic indexing attempt; current queue
state is reported by catalog queries.

One indexer takes a nonblocking advisory writer lock, copies the previous
snapshot to local temporary storage, and applies complete per-run transactions.
It closes/checkpoints DuckDB, publishes a new snapshot and atomically updates
`current.json`. Only then are the exact consumed requests acknowledged. Replayed
requests replace that run's indexed rows without duplicating them. Requests
arriving during an update remain pending for the next batch.

Queries open snapshots read-only and hold a separate shared cleanup guard.
Publishing a new generation does not wait for existing readers. Old snapshots
are pruned to the configured retention count when no reader is active; otherwise
cleanup is deferred. Kernel locks are released when a process exits. The mutable
DuckDB build lives in local temporary storage, not in a shared database file.

Automatic indexing runs after experiment termination, a CLI handoff update, or
a new note. It makes one attempt within the configured timeout. Pending requests
are retried by the next automatic update or `catalog --refresh ...`. For a large
backlog, each refresh handles at most `max_requests_per_refresh`; pending counts
remain visible. There is no perpetual retry loop. Stalled active runs require
live status inspection; a catalog state is only the last recorded observation.

The `smoke` and `verify` commands additionally require successful catalog
integration and save `catalog-validation.json`. A catalog validation failure
makes those validation commands fail while preserving the experiment's recorded
execution outcome. Ordinary `experiment` commands do not require catalog success.

Use `catalog refresh --rebuild` after a missing/damaged catalog, manually edited
records, or a schema upgrade. This scans all run directories within the configured
run/time bounds, including failed and incomplete runs. It leaves the previous
snapshot in place if any run cannot be collected. Missing metadata is represented
as a provenance gap. Source evidence and final-result promotion are unaffected.

The design assumes the shared filesystem provides working POSIX advisory locks
and atomic rename. Local multiprocess behavior is tested. Cross-node Slurm and
site-specific shared-filesystem semantics require validation on the target
cluster; the template does not claim those tests have been performed.

## Bounds, performance and extensions

Catalog limits are in `pipeline/conf/catalog.toml`. They cover update/query
timeouts, threads, memory, request batches, rebuild size, document/note limits,
returned rows and snapshot retention. The Pixi lockfile pins DuckDB. Scientific
parameters and comparison definitions stay in native experiment configurations.

Every query reports catalog freshness; list and SQL queries also report elapsed
milliseconds including opening the snapshot. Unit tests include 1,000 generated
run records and print measured query times. Those timings exclude Python/Pixi
startup, represent synthetic metadata, and do not predict large Parquet scan or
network-filesystem performance. The intended target is subsecond common summary
queries on representative local catalogs, not a guaranteed latency bound.

Keyword search has no external index to rebuild. The note IDs, source hashes,
text, version relationships and authors provide a foundation for later semantic
search. An embedding extension should retain its model/version and source-text
hash and cite original notes in retrieved results. Embeddings and a vector
service are not installed by this scaffold.
