# Experiment evidence

Each run retains its manifest, resolved configuration, terminal state, logs,
Nextflow trace/reports, outputs, audit and handoff. Failed runs remain here.
Expected outputs are defined by the invoked workflow. Payloads are ignored by Git.

The example publishes both CSV and Parquet. Research notes and handoff revisions
are retained under each run's `notes/`; `handoff.md` shows its current handoff.
`catalog-index.json` records the last automatic indexing attempt. A pending or
failed catalog update does not change the experiment's execution outcome.
