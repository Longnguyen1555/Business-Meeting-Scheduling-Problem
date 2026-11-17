# Research artifact

**Compact MaxSAT Encodings for Balanced Waiting in B2B Meeting Scheduling**

Tuyen Van Kieu, Long Duc Nguyen, Minh Duc Dang Vu, and Khanh Van To.
Corresponding author: Khanh Van To, khanhtv@vnu.edu.vn.

`JAIR_research_artifact_2026-11-17.zip` is prepared locally for distribution with
the manuscript. It has not yet been published as a GitHub Release and has no
permanent archive identifier. It contains code, benchmarks, recorded experiments,
all reference CSVs, and manuscript sources. The Git repository contains code,
benchmarks, configurations, and selected result CSVs.

## Start here

From the package root, run `python3 verify_snapshot.py` to check the distributed
files against `artifact_manifest.json`. Then open
`Business-Meeting-Scheduling-Problem/docs/reproduction.md`. When reading this
file in the Git repository, use [docs/reproduction.md](docs/reproduction.md).

The main command is `python src/Reproduce_JAIR_Review.py`, run from the code
directory after installing the pinned dependencies and extracting the matched
records. It reconstructs formula diagnostics and reported summaries without
executing a solver. The guide also covers compiling the manuscript and running
the manuscript audit and its 12 tests.

## Data and manuscript map

Paths below are relative to `Business-Meeting-Scheduling-Problem/` in the package.

| Evidence | Location |
| --- | --- |
| Complete matched records: 6,102 runs, schedules, provenance, validation reports | `outputs/journal/matched-encoding-v2-results/matched-encoding-v2-complete.tar.gz` |
| Published benchmark families and mapping | `data_table*/`, `instances_manifest.csv` |
| Derived precedence instances and generation metadata | `data_precedence_stress/`, `data_precedence_stress_high/` |
| Main quality and extended objective comparisons | `outputs/journal/bg-dinf-quality/`, `outputs/journal/all126-first/`, `outputs/journal/main-objectives-merged/`, `outputs/journal/final-tier-compact/` |
| Component ablations and supporting diagnostics | `outputs/journal/compact-objectives*/`, `outputs/journal/compact-ablation-stress-sessions/`, `outputs/journal/main-stress-sessions/` |
| All 29 reference CSVs and analysis metadata | `outputs/cross_generation_analysis/` |
| Pinned conference inputs | `journal_configs/conference_consistency_sources.json`, `frozen/conference/`, `output/ic12p-*/main/` |
| Computational source snapshot | `outputs/matched-encoding-v2-gcp.tar.gz` |
| Clean paper, source, bibliography, figures, checklist | Package-root `manuscript.pdf` and `JAIR_Example_Template/` |
| Conference manuscript for the consistency check | Package-root `manuscript/conference_101719.tex` |

Keep the package layout when reproducing the paper. Frozen conference exports
allow checks without Git history. The matched archive preserves environments
and solver-binary hashes; binaries are not included. Aggregation and
selected-repetition policies are in the reproduction guide and manuscript.

The shorter predecessor was accepted for presentation at RIVF 2026. A proceedings
record was unavailable when the manuscript was prepared; its existing preprint
is arXiv:2608.00614. Code uses the MIT license. Author-generated records use
CC BY 4.0 with exclusions in `DATA_LICENSE.md`; benchmark contents and third-party
materials retain their source terms.
