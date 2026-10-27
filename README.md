# Project Template

A small computational-research starter for a human and a coding agent. Pixi
manages the environment, Nextflow runs experiments, and ordinary Python checks
the resulting evidence. No hosted decision service is required. The agent rules,
skills and hooks are shared between Claude Code and Codex; see
[Working with an agent](#working-with-an-agent).

## First run

Install [Pixi](https://pixi.sh), then run these commands from this directory
on a Linux machine. The template includes a lockfile for Linux x86-64. Clone
with `git clone --recurse-submodules`, or run `git submodule update --init`
afterwards, to also check out the manuscript (see [Manuscript](#manuscript)).

```bash
pixi install --locked --manifest-path code/pixi.toml
pixi run --locked --manifest-path code/pixi.toml verify
```

`verify` runs the unit tests and then executes both processes of a small,
seeded Nextflow workflow. It checks the published data and summary, verifies
source hashes, and writes a run record. It uses local CPU resources and does
not submit Slurm jobs or download scientific datasets or model weights.
The example publishes both CSV and Parquet, then indexes its recorded evidence
and summary metrics into a local DuckDB catalog.
`smoke` and `verify` also check that this catalog contains the audited run and
write `catalog-validation.json` beside its other evidence.

Launch the example with a memorable, unused run identifier:

```bash
pixi run --locked --manifest-path code/pixi.toml experiment --run-id example-001 --config example.config
pixi run --locked --manifest-path code/pixi.toml status --run-id example-001
pixi run --locked --manifest-path code/pixi.toml audit --run-id example-001
pixi run --locked --manifest-path code/pixi.toml handoff --run-id example-001 --objective "Check the example workflow" --next-step "Review the summary before adding an analysis"
```

Existing run identifiers are never reused. `status` and `audit` are read-only.
The audit command exits nonzero when evidence is missing, corrupted, or comes
from an unsuccessful run. To diagnose a failure, keep its record and prepare a
new run after a reasoned correction. Automatic Nextflow resume is intentionally
not exposed by this starter.

Ordinary experiments require a Git repository with an initial commit and a
configured author identity. After every terminal experiment, the runner commits
the frozen source snapshot to `experiment/<run-id>`. It uses a temporary index
and never checks out that branch, so concurrent runs do not switch or stage the
shared worktree. Before launching another experiment, the runner creates or
verifies the newest prior experiment checkpoint and refuses the launch when that
cannot be done. `smoke` and `verify` are validation runs and do not create
experiment branches. The run's `git-checkpoint.json` records the branch, commit
and source digest; `status` includes that record.

## Layout

`.agents/layout.toml` is the machine-checked version of this layout. Run
`pixi run --locked --manifest-path code/pixi.toml check-layout` to compare the
checkout with it, and add `--fix` to remove caches and stale worktree entries.
The check never moves or deletes anything else; the `cleanup-layout` skill
describes how to resolve each finding. Update the contract and this section
together when the structure changes on purpose.

```text
AGENTS.md                         Shared research and coding rules (all agents)
.agents/skills/                   Shared task procedures (all agents)
.agents/layout.toml               Directory layout contract (check-layout)
CLAUDE.md                         Claude Code adapter: imports AGENTS.md
.claude/skills -> ../.agents/skills  Claude Code skill discovery (symlink)
.claude/settings.example.json     Optional Claude Code hooks
.codex/hooks.example.json         Optional Codex hooks
code/
  pixi.toml, pixi.lock            Environment and commands
  pipeline/
    main.nf, nextflow.config      Orchestration and execution profiles
    conf/                        Native scientific and execution settings
    modules/, subworkflows/       Processes and reusable compositions
    bin/
      pyproject.toml             Installable Python package
      research_project/          CLI and task implementation
      tests/                     Generated-fixture unit tests
  data/raw/                      Immutable inputs
  data/store/                    Reusable derived checkpoints
    catalog/                     Rebuildable DuckDB snapshots and pending requests
  launches/<run_id>/              Frozen code, config and launch state
  work/<run_id>/                  Nextflow-managed working files
  results/runs/<run_id>/          Outputs, logs, provenance and handoff
  results/final/                 Explicitly promoted results
  docs/                          Project decisions and setup notes
  .worktrees/<name>/              Git worktrees (source only; ignored by Git)
manuscript/                      Publication material (separate repo, submodule)
```

## Make it your project

1. Replace the title and scientific objective. The internal `research_project`
   package name can stay until a rename is useful.
2. Put scientific settings and execution limits in `code/pipeline/conf/`.
   The runner freezes these files and adds run-specific paths in
   `code/launches/<run_id>/execution.config`.
3. Add task implementation under `pipeline/bin/research_project/`, processes
   under `modules/`, and channel connections under `subworkflows/` and `main.nf`.
4. Update `expected_outputs` in the selected configuration and the workflow's
   content checks in `runner.py`. An exit code alone is not an evidence audit.
5. Keep a small synthetic configuration exercising the same process graph.
   Run `verify` after orchestration changes.
6. Declare `catalog_metrics`, dataset/split identifiers, biological units and
   evaluation definitions in the experiment configuration. Keep catalog resource
   limits in `pipeline/conf/catalog.toml`.

The example generates random numbers solely to demonstrate seeded execution
and artifact verification. It is not an analysis method or scientific result.

## Manuscript

`manuscript/` is its own Git repository, checked out as a submodule, so the
paper keeps an independent history (and can later sync with Overleaf or a Git
host) while the project records which manuscript commit it refers to.

- Commit manuscript changes inside `manuscript/`, then commit the updated
  submodule pointer in the project. Uncommitted or unrecorded manuscript
  changes make the project tree dirty, so agents ask about them before a launch.
- The manuscript has no remote yet. `.gitmodules` records
  `../project-template-manuscript.git`, so it belongs next to the project's own
  repository, for example `OWNER/project-template` and `OWNER/project-template-manuscript`.
  Create both as empty repositories, then:

  ```bash
  git remote add origin git@github.com:OWNER/project-template.git
  git submodule sync                    # manuscript origin -> OWNER/project-template-manuscript
  git -C manuscript push -u origin main
  git push -u origin main
  ```

  To keep the manuscript elsewhere, such as an Overleaf Git URL, run
  `git submodule set-url manuscript URL` instead of `git submodule sync`; it also
  points the manuscript's `origin` at that URL. Commit the changed `.gitmodules`.

- LaTeX build files are ignored by the manuscript's own `.gitignore`; the
  project's `.gitignore` does not apply inside a submodule.

## Query research state

Completed and failed runs automatically request a bounded catalog update.
Each experiment writes to its own run directory; one indexer publishes immutable
DuckDB snapshots. An indexing error leaves the results and request available for
recovery and does not change the experiment's exit status. There is no database
server or background service to maintain.

```bash
pixi run --locked --manifest-path code/pixi.toml catalog runs
pixi run --locked --manifest-path code/pixi.toml catalog evidence --run-id example-001
pixi run --locked --manifest-path code/pixi.toml catalog query --sql "SELECT run_id, name, value, unit FROM metrics ORDER BY run_id, name"
pixi run --locked --manifest-path code/pixi.toml catalog parquet --run-id example-001 --path example/samples.parquet --sql "SELECT count(*) AS n, avg(value) AS mean FROM result"
pixi run --locked --manifest-path code/pixi.toml catalog note --run-id example-001 --kind decision --author researcher --text "Review the seeded example before defining a scientific analysis."
pixi run --locked --manifest-path code/pixi.toml catalog search --query "seeded example"
```

Query commands return JSON with a snapshot timestamp, pending-request count and
source references. They normally read the latest completed snapshot. To catch up
first, use `catalog --refresh runs`. To recreate the catalog from run records,
use `catalog refresh --rebuild`. Both updates have a configured time limit.

Handoffs and research notes retain their revisions. Keyword search is included;
embedding models and semantic indexes can be added later. See
[the catalog guide](code/docs/catalog.md) for the schema, comparison rules,
recovery behavior and concurrency limits.

## HPC execution

Edit `code/pipeline/conf/slurm.config` for your site's partition, account and
resource policy, then run the driver in an allocation or persistent session
supported by your HPC:

```bash
pixi run --locked --manifest-path code/pixi.toml experiment --run-id hpc-example-001 --config example.config --profile slurm
```

Nextflow submits the individual processes. The wrapper's finite driver timeout
comes from the selected native configuration. On timeout it terminates the
local Nextflow process group and records the failure. After a driver/node kill,
inspect `squeue`/`sacct` for surviving scheduler jobs before any new launch;
the starter cannot guarantee cancellation after a cluster or host failure.

## Working with an agent

Start with `AGENTS.md`. The project skills cover experiment execution,
validation, evidence review, session handoffs, evidence-traced manuscript
writing (`write-from-evidence`) and layout cleanup (`cleanup-layout`). A handoff points to the
authoritative run record and includes an explicit next action. Refresh job and
artifact status when resuming; a saved note can be stale.
Use the catalog for recorded results and evidence discovery. When answering a
question about the newest results, inspect its pending count and refresh once
if needed. Report remaining pending records or provenance gaps. Catalog entries
do not replace a fresh run audit or live scheduler status.

### Agent support

The project is agent-agnostic: rules, procedures and hook logic each have one
shared home, and each agent gets a thin adapter that points to it.

| Shared source | Claude Code | Codex |
| --- | --- | --- |
| `AGENTS.md` rules | `CLAUDE.md` imports it with `@AGENTS.md` | Read natively |
| `.agents/skills/` procedures | `.claude/skills` symlink | Read natively |
| Pixi hook tasks | `.claude/settings.example.json` | `.codex/hooks.example.json` |

Other tools that read `AGENTS.md` (for example Cursor or GitHub Copilot) get the
shared rules without any extra files. Put new rules in `AGENTS.md` and new
procedures in `.agents/skills/<name>/SKILL.md`, never only in one agent's
adapter. The `test_agent_adapters.py` tests fail if the adapters drift apart.
On Windows, enable Git symlinks (`core.symlinks`) or copy the skills directory.

### Optional hooks

Both hook examples run the same six Pixi tasks:

| Event | Task | Behavior |
| --- | --- | --- |
| Session start | `latest-handoff` | Loads the most recent handoff into context |
| Session start | `hook-layout-start` | Adds any layout problems (including orphaned run records) to the agent's context |
| Before an edit | `hook-pre-edit` | Checkpoints the previous experiment before workflow or experiment-design edits; blocks the edit if that fails |
| Before a shell command | `hook-pre-shell` | Pauses an `experiment` launch on a dirty Git tree and tells the agent to list the changes and ask the user whether to commit |
| After an edit | `hook-post-edit` | Runs Ruff on edited Python files |
| End of a turn | `hook-layout-stop` | Once per turn, sends the agent back to resolve stray or misplaced files, missing contract paths, broken adapters or misplaced worktrees |

The hook module reads both agents' edit events: Codex `apply_patch`, and
Claude Code `Edit`, `MultiEdit` and `Write`. Edits made through shell commands
are not intercepted. The runner independently enforces the same checkpoint
before launch, including launches made without an agent. The explicit
`latest-handoff` and `format-code` tasks work without hooks. Pixi must be on the
agent's PATH.

`hook-pre-shell` recognizes `pixi run … experiment` and
`python -m research_project.runner run`, including inside `bash -c` scripts.
The agent commits only with your approval. If you decline, it relaunches with an
`ALLOW_DIRTY_LAUNCH=1` prefix, and the run manifest records `git.dirty: true`.
The frozen snapshot still captures uncommitted pipeline and configuration files. The hook never
blocks other commands, and it allows the launch when Git is unavailable, leaving
the runner's own Git checks in charge. Launches you type yourself are not checked.

Hooks are off until you review and enable them:

- **Claude Code:** merge the `hooks` block from `.claude/settings.example.json`
  into `.claude/settings.local.json` (personal, ignored by Git) or
  `.claude/settings.json` (shared with collaborators). Check it with `/hooks`.
- **Codex:** copy `.codex/hooks.example.json` to `.codex/hooks.json` (ignored by
  Git), then review and trust it through Codex's `/hooks` interface. Start Codex
  in the project root; its commands use root-relative paths.

To support another agent, add its instruction adapter (if it does not read
`AGENTS.md`), expose `.agents/skills/`, and point its hooks at the same Pixi
tasks. Extend `research_project/agent_events.py` if its edit events use
different tool names or fields.

### Worktrees

Parallel work uses Git worktrees under `code/.worktrees/<name>/`:

```bash
pixi run --locked --manifest-path code/pixi.toml worktree add NAME      # branch worktree/NAME from HEAD
pixi run --locked --manifest-path code/pixi.toml worktree add NAME --branch BRANCH --base REF
pixi run --locked --manifest-path code/pixi.toml worktree list
pixi run --locked --manifest-path code/pixi.toml worktree remove NAME   # keeps the branch
```

`add` also installs the worktree's own locked Pixi environment, so its tasks
run its own source; `--no-install` skips that. `remove` refuses a worktree with
uncommitted or untracked changes.

A worktree holds source only. Runs launched from it (experiments and
`smoke`/`verify`) freeze that worktree's source but are recorded in the main
checkout: `code/launches/`, `code/work/`, `code/results/runs/` and the catalog
in `code/data/store/`. `status`, `audit`, `handoff` and `catalog` read the same
records from any checkout, and removing a worktree loses no evidence. The
manifest's `git` block records the launching checkout, branch, revision and
dirty state. The `experiment/<run-id>` checkpoint branch starts from that
worktree revision.

- **Raw data:** the runner passes `params.data_dir` (the main checkout's
  `code/data`), so read raw inputs through it rather than paths relative to the
  checkout.
- **One experiment at a time:** as before, a launch waits for the previous
  experiment to reach a terminal state, now across all checkouts.
- **Shared indexing:** the main checkout's code updates the shared catalog, even
  for runs launched from a worktree.
- **Manuscript:** the `manuscript/` submodule stays empty in worktrees; edit it
  in the main checkout. If you initialize it inside a worktree, Git refuses to
  remove that worktree until any manuscript commits made there are kept and it
  is removed with `--force`.

**Claude Code** uses `code/.worktrees/` once the `WorktreeCreate` and
`WorktreeRemove` hooks from `.claude/settings.example.json` are enabled. Without
them it uses `.claude/worktrees/`. **Codex**'s `--worktree` flag manages its own
location, so create the worktree with `worktree add NAME` and start Codex in it
(`codex -C code/.worktrees/NAME`).

Scientific settings remain in Nextflow configuration, not in agent memory.
Reusable lessons belong in a reviewed skill or project note. There are no
automatic stashes, new model services, or perpetual retry loops. Experiment
commits are limited to frozen source snapshots on `experiment/<run-id>` branches.

## Updating dependencies

Edit `code/pixi.toml`, run `pixi lock --manifest-path code/pixi.toml`, and review
the lockfile with the manifest. Then run `verify`. The runner invokes the
active Pixi environment and records its interpreter plus the frozen lockfile;
the source snapshot does not contain a second installed environment.

The structure takes inspiration from the task procedures, verification reports
and session continuity in [Everything Claude Code](https://github.com/worldflowai/everything-claude-code).
The implementation here is written for this template and its research workflow.
