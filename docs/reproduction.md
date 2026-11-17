# Reproducing the manuscript results

The Git repository provides code, benchmarks, configurations, tests, and 27
reference CSVs. Full analysis and manuscript checks use the separate
`JAIR_research_artifact_2026-11-17.zip`. This package is prepared locally; it is
not yet a published GitHub Release and has no permanent archive identifier.

## Package layout

Keep these sibling directories together after extraction:

```text
JAIR_research_artifact_2026-11-17/
  artifact_manifest.json
  verify_snapshot.py
  manuscript.pdf
  JAIR_Example_Template/
  Business-Meeting-Scheduling-Problem/
  manuscript/conference_101719.tex
```

The analysis uses this layout to compare generated LaTeX rows and pinned
conference records. A plain Git clone contains neither the raw runs nor these
manuscript inputs. The package is self-contained for analysis and does not
require Git history or access to the original experiment machines.

## Install and check the data

Use Python 3.10 or later; package verification uses Python 3.12. From the extracted
package root:

```bash
python3 verify_snapshot.py
cd Business-Meeting-Scheduling-Problem
python3 -m venv .venv-review
source .venv-review/bin/activate
python -m pip install -r src/requirements.txt
tar -xzf outputs/journal/matched-encoding-v2-results/matched-encoding-v2-complete.tar.gz -C outputs/journal
```

The dependencies are `python-sat==1.8.dev25` and `psutil==7.2.2`. The manifest
checks files as distributed; run it before generating outputs. The matched
archive contains raw JSONL, normalized CSVs, schedules, commands, environments,
and checksums. Solver binaries are not included or needed for this analysis.

## Rebuild summaries and formula diagnostics

From the code directory, with the environment activated:

```bash
python src/Reproduce_JAIR_Review.py
```

This reconstructs formula counts from instances and aggregates recorded runs.
It compares all 29 generated CSVs and generated LaTeX rows with their references
and checks the main quality sets (84/47/21) and extended comparisons (62/83).
Formula construction can take several minutes; no solver optimization is
performed. Outputs go to `outputs/reproduced_review/`.

For a quick check that omits the independent formula reconstruction:

```bash
python src/Reproduce_JAIR_Review.py --skip-formula-rebuild
```

Use `--output-dir outputs/my-analysis` to retain an earlier check. The reference
directory remains unchanged. The package's `remaining_runs.csv` and
`reuse_inventory.csv` are historical planning indices; the completed matrix
contains all 6,102 runs.

## Build and check the paper

Install TeX Live with Biber, latexmk, TikZ, PGFPlots, and the packages requested
by the manuscript. The prepared package was built with TeX Live 2025. After
the default reproduction above:

```bash
cd ../JAIR_Example_Template
python3 figures/prepare_tikz_data.py --check
latexmk -pdf -interaction=nonstopmode -halt-on-error JAIR_Example_Template.tex
cd ../Business-Meeting-Scheduling-Problem
python src/Audit_JAIR_Update.py --analysis outputs/reproduced_review --report outputs/reproduced_review/manuscript_audit.json
python -m unittest discover -s tests -p test_audit_jair_update.py -v
```

For a different reproduction directory, supply it to `--analysis`. The audit
compares paper values and highlighting with the data, including four appendix
result tables. Its 12 tests also detect altered values, labels, and highlighting.
Compile the paper first and use the full formula reconstruction for this audit.
Other model tests can invoke a SAT backend; they are separate from this
recorded-data workflow.

## Interpreting the results

There are 6,102 runs: 226 variants × three encodings × three objectives × three
repetitions. All-126, Stress, and Stress-high contain 126, 60, and 40 variants,
respectively, derived from 20 base-instance groups.

An instance is OPT if any repetition proves optimality; solved includes OPT and
proven UNSAT. Runtime and peak RSS use the median of OPTIMAL repetitions for an
OPT instance, otherwise all three runs, followed by medians across instances.
PAR-2 assigns 14,400 seconds to a timeout before taking the per-instance median
and averaging instances. Paired runtime ratios require three OPTIMAL repetitions
in both encodings.

Schedule comparisons select the lowest-numbered OPTIMAL repetition. The idle
range measures balance among participants with at least two meetings. Population
standard deviation describes their idle totals within that selected schedule;
it measures neither variability between repetitions nor all optimal schedules.
The comparisons are descriptive results for this benchmark family.

## New computational experiments

`journal_configs/matched_encoding_evaluation_v2.json` specifies the matched
design; `src/Journal_Experiment.py` implements the runner. Shell launchers are
retained for compatibility. Small smoke, pilot, and warmup configurations remain
because tests and launchers reference them; their outputs are excluded from Git.

The package's `outputs/matched-encoding-v2-gcp.tar.gz` preserves the computational
source used in the completed campaign, distinct from later analysis edits.
Runs used a 7,200-second cutoff and the common c4-highcpu-8 resource profile.
UWrMaxSAT was invoked with `-m`; thread and seed fields are runner metadata,
rather than explicit solver options. Records retain actual solver hashes,
Python versions, commands, and reuse lineage. See the
[campaign note](../MATCHED_EVALUATION_GCP.md) for more detail.

## Licenses and sources

Code is MIT-licensed. Author-generated records, analysis, and stress-generation
metadata use CC BY 4.0 with the exclusions in `DATA_LICENSE.md`. Benchmark contents
and third-party styles and software retain their source terms. Benchmarks are
from Bofill et al., JAIR 2022, DOI 10.1613/jair.1.12670. The shorter predecessor
was accepted for presentation at RIVF 2026; its preprint is arXiv:2608.00614.
