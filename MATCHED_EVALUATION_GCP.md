# Matched encoding evaluation: three identical GCP VMs

This campaign completes three repetitions for Direct, Compact-Base, and
Compact-Objective under BG-d2, IR, and IR-IM-IS on all three benchmark sets.
It contains 6,102 target cells, 4,960 validated reused cells, and 1,142 new runs.
All three VMs use the same frozen source snapshot and one worker each.

| VM | Shard index | Reused cells | New runs | Final cells |
|---|---:|---:|---:|---:|
| VM 1 | 0 | 1,670 | 382 | 2,052 |
| VM 2 | 1 | 1,646 | 379 | 2,025 |
| VM 3 | 2 | 1,644 | 381 | 2,025 |
| Total | | 4,960 | 1,142 | 6,102 |

The 64 missing Stress-high records from the latest M05/M06 reruns are already
included in these counts. All 68 existing rows from each partial rerun are
retained. Original result files are never rewritten.

## Prepared package

The ready-to-run package is committed at
[`outputs/matched-encoding-v2-gcp.tar.gz`](outputs/matched-encoding-v2-gcp.tar.gz).
It includes the launcher, the complete tested runtime source snapshot, and the
validated reuse bundle. The computational source in this package is frozen
against base commit `6e5f7d7`; the documentation commit publishing the package
is not the runtime base commit.

Preparation has validated schedules and proven vectors, checked static formula
identity, recovered missing assignment vectors from `schedule_by_slot`, and
frozen source-file hashes and the base Git commit. It has also materialized the
partial M05/M06 CSVs in the derived bundle, without invoking a solver.

The bundle is `outputs/journal/matched-encoding-v2`. It includes `campaign.json`,
`plan.json`, `reuse_map.csv`, `remaining_runs.csv`, `reused_records.jsonl`,
`supplemental_runs.csv`, recovered source CSVs, and `preparation_report.json`.

The launcher automatically installs the exact implementation and bundle in a
separate worktree at commit `6e5f7d7`. Preparation does not need to be repeated
on the VMs, and the historical result directories need not be copied.
`src/Matched_Encoding_Evaluation.py` is also published as a source file; its
command-line entry point delegates to the same frozen runtime.

## Install the same package on each VM

From the existing repository on each VM, update the launcher branch:

```bash
git fetch origin journal-compact-objectives
git switch journal-compact-objectives
git pull --ff-only origin journal-compact-objectives
```

Stay in this repository when invoking the commands below. The launcher reads
`journal_gcp.env` here and automatically uses `.venv-ubuntu/bin/python` when it
exists. For environments in another location, set the corresponding paths:

```bash
export JOURNAL_ENV_FILE=/path/to/original-repository/journal_gcp.env
export JOURNAL_PYTHON=/path/to/original-repository/.venv-ubuntu/bin/python
```

Check the plan on every VM:

```bash
bash scripts/run_matched_encoding_three_vms.sh plan
```

On first use this creates `../b2b-matched-v2`, verifies the package SHA-256,
extracts all dependencies and reuse records, then verifies the frozen source and
bundle hashes. It reports 6,102 planned cells, 4,960 reused cells, and 1,142 new
runs. Subsequent invocations verify and reuse this directory without replacing
source or results. Set `MATCHED_RUNTIME_DIR` to another absolute directory
outside the repository if needed. An existing manually installed runtime with
the same hashes can be reused.

The frozen runtime's computational source differs from its base tree by design.
The runner permits these exported files and independently requires their exact
hashes and the same base Git commit. Keep this source snapshot unchanged until
all three shards finish. No manual extraction or copying of Python files is
required.

The new runs pin UWrMaxSAT to
`29c51abeabd43015c75a8e6ee789fdbb9aa2803096d2cac536ec60bd516ab4a7`.
Set `UWRMAXSAT_BIN` and `UWRMAXSAT_SHA256` in the VM configuration to this binary.
Historical solver hashes are retained as descriptive provenance under the
author's common-GCP assumption. Each new run has a 7,200-second timeout, one
thread and seed zero. `CPU_CORE` from the VM configuration is honored when set.

## Run exactly one command per VM

VM 1:

```bash
bash scripts/run_matched_encoding_three_vms.sh 0
```

VM 2:

```bash
bash scripts/run_matched_encoding_three_vms.sh 1
```

VM 3:

```bash
bash scripts/run_matched_encoding_three_vms.sh 2
```

Run in an existing persistent terminal or `tmux` session. Each shard is
append-only and has its own worker lock. Re-running the same command resumes
only that shard's missing jobs. Completed OPTIMAL, UNSAT, and TIMEOUT rows are
retained; a valid timeout is not retried. If an actual ERROR occurs, inspect its
log before deliberately retrying with `RETRY_ERRORS=1`.

Output directories are under the frozen runtime, by default
`../b2b-matched-v2/outputs/journal/matched-encoding-v2/shard-0-of-3`,
`shard-1-of-3`, and `shard-2-of-3`. Each contains the same plan, its own reused
and executed JSONL records, normalized CSV, environment, status and logs.
Re-run commands from the original launcher repository after interruption.

## Validate, return and merge

### Completion after splitting shard 0

Shard 0 was safely interrupted with 1,953 terminal records and 99 remaining
cells. Its stopped source is retained in
`outputs/journal/matched-encoding-v2-results/shard-0-of-3.stopped-1953.tar.gz`.
The plan hash and its 6,102 target cells are unchanged. Partition metadata is
refined from three shards to nine, preserving the original execution records
and source hashes in the derived copies.

| Original shard | New indices | Execution |
|---|---|---|
| 0 | 0, 3, 6 | Remaining cells run on three VMs |
| 1 | 1, 4, 7 | Completed records copied without solving |
| 2 | 2, 5, 8 | Completed records copied without solving |

The active worker sessions are `matched-split-0`, `matched-split-3` and
`matched-split-6`. Each writes to
`outputs/journal/matched-encoding-v2-split/shard-INDEX-of-9` inside its VM's
frozen worktree. Continue these workers with the recorded `--shard-count 9`
and index. Starting the old original shard 0 would duplicate work.

Part 3 moved to the VM running part 0 on 9 November, at the author's request.
Parts 0 and 3 now run concurrently, each with one thread, on
`instance-20261104-105839` in project `project-e19a1072-60cf-47ea-a1e`.
The part 3 session is `matched-split-3`, owned by `duc933827`. Its output path
is `/home/duc933827/b2b-matched-v2/outputs/journal/matched-encoding-v2-migrated/shard-3-of-9`.
Its 672 completed records and the previous execution environment were copied
unchanged. Three pending cells resume on the new VM. The interrupted active
cell has no terminal record and restarts within those three cells.

The old part 3 worker and finalizer on `instance-20261031-120104` were stopped.
The original data remain there; a verified stopped snapshot is also stored at
`outputs/journal/matched-encoding-v2-results/shard-3-of-9.stopped-672.tar.gz`.
The exact original solver binary was transferred with SHA-256
`f9c0f7fac1890b83dde1dac28c8bb0cd7db8f2018df9bf675f5ef5fff03b0b92`.
Its private runtime copy is outside `src`; the computational source and plan
hash were verified before resuming. The corresponding copied campaign
configuration is in `matched-encoding-v2-migrated/campaign.json`.
The resumed environment records the VM transition. Original logs are retained
under `provenance/pre-migration-logs`; the new worker writes
`worker.after-migration.log`. Migration evidence is in
`outputs/journal/matched-encoding-v2-results/part-3-migration.receipt.json`.

The sessions `matched-export-0`, `matched-export-3` and `matched-export-6`
wait for the corresponding worker PID to exit. They verify complete terminal
coverage, preserve the campaign configuration matching the worker's recorded
SHA-256, and export `shard-INDEX-of-9.tar.gz` into the original repository's
`outputs/journal/matched-encoding-v2-results`. They stop on ERROR, interruption,
missing cells or changed configuration. They do not retry timeouts, run solvers,
or commit files. Archive transfer and publication are separate steps.
`matched-export-3` now runs beside the migrated part 3 worker on
`instance-20261104-105839` and exports from its new output path.

For a manual export after a worker finishes, use the updated launcher in the
original repository, substituting the actual source path:

```bash
bash scripts/run_matched_encoding_three_vms.sh export 3 --shard-count 9 \
  --source /home/duc933827/b2b-matched-v2/outputs/journal/matched-encoding-v2-migrated/shard-3-of-9
```

Completed derived partitions also need `environment.json` and a normalized CSV.
The updated split helper creates these from the parent environment for entirely
copied partitions, preserving the parent environment under `provenance/`.
Partial partitions receive a new execution environment when their worker starts.
Each merge input includes the verified executed configuration at
`provenance/campaign.json`. A configuration hash difference is accepted only
under the common-GCP policy when these hash-verified files differ solely in
`uwrmaxsat_sha256`. Other configuration differences still prevent merging.

The author machine has prepared partitions 1, 4, 7 from the shard 1 archive and
2, 5, 8 from the shard 2 archive. Their independently validated source schedules
and the stopped parent snapshot are recorded in
`outputs/journal/matched-encoding-v2-returns/preparation_audit.json`.
Merge all nine partitions only after 0, 3 and 6 finish; the three-input launcher
shortcut is for the original partition layout. Use the author's updated
`src/Merge_Journal_Shards.py` with nine `--input` arguments and
`--allow-dirty --allow-mixed-environments` for the refined layout.

### Publishing an original three-way shard

The frozen worktree uses a detached base commit. Publish results from the
original launcher repository on `journal-compact-objectives`.

After the VM's worker has finished, update the original repository and export
its shard. The example is for VM 1; use `1` on VM 2 and `2` on VM 3:

```bash
git pull --ff-only origin journal-compact-objectives
bash scripts/run_matched_encoding_three_vms.sh export 0
git add -- outputs/journal/matched-encoding-v2-results/shard-0-of-3.tar.gz
git commit -m "Add matched evaluation results for shard 0"
git pull --rebase origin journal-compact-objectives
git push origin HEAD:journal-compact-objectives
```

Export reads the existing runtime directly without bootstrapping, restoring
files, applying a solver pin, or invoking a solver. It copies every shard file
including JSONL, CSV, environment, reports and ignored `.log` files into the
archive, excluding only the worker lock. Source files remain in place. SHA-256
hashes for every file and record coverage are included in `export_manifest.json`.
Independent schedule and objective validation still follows after transfer.
Export refuses an active worker, missing or nonterminal cells, and replacement
of a differing archive. A repeated export of identical files is accepted.

If the runtime directory is custom, use
`export 0 --source /actual/path/to/shard-0-of-3`. To preserve partial or ERROR
results for diagnosis, add `--allow-incomplete`; the manifest records that the
shard is incomplete. A later changed snapshot needs a new `--archive PATH`.

After all three VM commits have been pushed, pull them on the author machine
and extract the archives into an empty results directory, for example:

```bash
mkdir -p outputs/journal/matched-encoding-v2-returned
for index in 0 1 2; do
  tar -xzf "outputs/journal/matched-encoding-v2-results/shard-$index-of-3.tar.gz" \
    -C outputs/journal/matched-encoding-v2-returned
done
```

On each VM, validate its own shard after completion; substitute its index:

```bash
bash scripts/run_matched_encoding_three_vms.sh validate 0
```

The validator permits the other two VMs' cells to be absent. The merger
still requires every cell assigned to each supplied shard to be present.
Transfer the three shard directories back to the author machine without
combining or replacing their JSONL files by hand, then run from the launcher
repository, substituting the actual paths to the returned directories:

```bash
bash scripts/run_matched_encoding_three_vms.sh merge \
  /path/to/returned/shard-0-of-3 \
  /path/to/returned/shard-1-of-3 \
  /path/to/returned/shard-2-of-3
bash scripts/run_matched_encoding_three_vms.sh analyze
```

Analysis refuses to update publication outputs until all 6,102 cells are
complete and validated. It checks schedule feasibility, objective agreement,
static formula invariance, repetition coverage and distinct execution identity.

Runtime and RSS are collapsed to per-instance medians over three executions.
Each run's timeout is penalized before taking its per-instance PAR-2 median;
those medians are then averaged. OPT counts instances with at least one proof
of optimality; `repetition_outcomes.csv` retains all three statuses and costs.
Runtime ratios require all three repetitions to be OPTIMAL in both encodings.
The earliest optimal repetition supplies schedule-quality metrics.

The analyzer writes the three-repetition LaTeX rows and the source data used by
the figures. The figure generator reads the analysis completion flag before
switching from single-run to three-repetition runtime data. Main-text prose and
captions must then be reconciled with the completed numerical results, followed
by PDF compilation and rendered figure QA. Until that point the current
manuscript tables remain on their validated single-run results.

The 15-configuration ablation remains descriptive for runtime; its formula
counts, BG-d-infinity schedules, and final-tier results need no additional
repetitions in this campaign.
