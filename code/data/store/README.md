# Reusable derived checkpoints

Keep expensive derived products here with input, code and configuration hashes.
Record which checkpoint each run consumed. Generated payloads are ignored by Git.

`catalog/` holds derived DuckDB snapshots, their atomic current pointer, pending
index requests and coordination locks. The catalog is rebuilt from run evidence;
see [the catalog guide](../../docs/catalog.md). Preserve original run records and
versioned research notes when archiving a project.
