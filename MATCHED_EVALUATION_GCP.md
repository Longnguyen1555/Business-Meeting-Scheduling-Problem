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

The exported package contains the exact implementation files and this bundle.
It is intended for a separate checkout of commit `6e5f7d7`, preserving the
existing code and result directories on each VM. Preparation does not need to
be repeated on the VMs, and the historical result directories need not be copied.

## Install the same package on each VM

From the existing repository on each VM, fetch the branch, create a separate
runtime checkout of the frozen base, and extract the package from the fetched
GitHub branch:

```bash
git fetch origin journal-compact-objectives
git worktree add --detach ../b2b-matched-v2 6e5f7d7
git archive origin/journal-compact-objectives \
  outputs/matched-encoding-v2-gcp.tar.gz | tar -xf - -C ../b2b-matched-v2
cd ../b2b-matched-v2
tar -xzf outputs/matched-encoding-v2-gcp.tar.gz
```

Create this checkout only once per VM. After interruption, return to
`../b2b-matched-v2` and repeat only the environment setup and that VM's run
command. The exported computational source differs from the base tree by
design; its exact hashes are recorded in the bundle and checked by the runner.

Set the paths to the already installed Python environment and experiment
configuration on that VM. Replace the example paths with their actual paths:

```bash
export JOURNAL_ENV_FILE=/path/to/original-repository/journal_gcp.env
export JOURNAL_PYTHON=/path/to/original-repository/.venv-ubuntu/bin/python
export ALLOW_DIRTY=1
bash scripts/run_matched_encoding_three_vms.sh plan
```

`ALLOW_DIRTY=1` permits the exported implementation files over the base commit.
The runner independently requires the exact frozen hashes of all Python source
files and the same base Git commit. Modified computational source files are
rejected even with this flag. Keep this source snapshot unchanged until all
three shards finish.

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

Output directories are `outputs/journal/matched-encoding-v2/shard-0-of-3`,
`shard-1-of-3`, and `shard-2-of-3`. Each contains the same plan, its own reused
and executed JSONL records, normalized CSV, environment, status and logs.

## Validate, return and merge

On each VM, validate its own shard after completion; substitute its index:

```bash
"$JOURNAL_PYTHON" src/Validate_Journal_Run.py \
  --output outputs/journal/matched-encoding-v2/shard-0-of-3 \
  --allow-incomplete --allow-dirty --allow-environment-drift
```

`--allow-incomplete` permits the other two VMs' cells to be absent. The merger
still requires every cell assigned to each supplied shard to be present.
Transfer the three shard directories back to the author machine without
combining or replacing their JSONL files by hand, then run:

```bash
bash scripts/run_matched_encoding_three_vms.sh merge \
  outputs/journal/matched-encoding-v2/shard-0-of-3 \
  outputs/journal/matched-encoding-v2/shard-1-of-3 \
  outputs/journal/matched-encoding-v2/shard-2-of-3
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
