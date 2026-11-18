# Balanced waiting in B2B meeting scheduling

Code, benchmarks, recorded experiments, and results for **Compact MaxSAT Encodings
for Balanced Waiting in B2B Meeting Scheduling**, by Tuyen Van Kieu, Long Duc
Nguyen, Minh Duc Dang Vu, and Khanh Van To.

The study refines balanced waiting lexicographically: minimize participant idle
range, then maximum idle, then total idle. Balance is measured over participants
with at least two meetings. Compact-Base is the inherited compact feasibility
encoding; Compact-Objective adds objective-specific representations. Direct is an
independent implementation based on the 2022 MaxSAT formulation, with the
adaptations described in the article. Encoding size, schedule quality, and solver
effort are evaluated separately.

## Repository contents

| Location | Contents |
| --- | --- |
| `src/`, `tests/` | Encodings, generators, experiment runners, analysis, and tests |
| `journal_configs/`, `scripts/` | Experiment configurations and launchers |
| `data_table*/`, `instances_manifest.csv` | Published benchmark families and instance identities |
| `data_precedence_stress*/` | Derived instances, generation metadata, manifests, and witness schedules |
| `outputs/journal/` | Individual run results, execution metadata, and objective and ablation studies |
| `outputs/cross_generation_analysis/` | 32 reference CSVs, analysis metadata, and LaTeX result rows |
| `frozen/`, `output/` | Historical inputs used for the conference comparison |
| `outputs/matched-encoding-v2-gcp.tar.gz` | Source and configuration snapshot used for the matched campaign |

The frozen conference manuscript is a historical input to the reference
comparison. Its original bytes and source-data checksums are retained.

## Reproduce the reported results

Use Python 3.10 or later. From this repository's root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
python src/Verify_Reproduction_Inputs.py
tar -xzf outputs/journal/matched-encoding-v2-results/matched-encoding-v2-complete.tar.gz -C outputs/journal
python src/Reproduce_JAIR_Review.py
```

Dependencies are pinned to `python-sat==1.8.dev25` and `psutil==7.2.2`.
The final command reconstructs formula counts from the instances, recomputes
summaries from the recorded experiments, and compares all 32 reference CSVs and
the LaTeX result rows byte for byte. It also checks the extended objective
comparisons and runtime-environment sensitivity. This analysis takes several
minutes and does not run a MaxSAT optimizer.

Outputs go to `outputs/reproduced_review/`. Use `--output-dir` to choose another
directory, or `--skip-formula-rebuild` for a quicker check of the recorded
results. `--reference-latex` selects an alternative reference for the LaTeX rows.
The default reference is inside this repository; no separate manuscript folder
or supplementary download is needed.

Run the focused analysis tests with:

```bash
python -m unittest discover -s tests -p 'test_cross_generation_analysis.py'
python -m unittest discover -s tests -p 'test_objective_priority_analysis.py'
python -m unittest discover -s tests -p 'test_conference_consistency.py'
```

The configurations and launchers document new solver experiments, which require
the corresponding solver binaries and computing environment. The frozen source
snapshot preserves the implementation used for the recorded campaign.

## Experimental settings and interpretation

The matched study contains 6,102 runs: 226 variants, three encodings, three
objectives, and three repetitions. These variants share 20 base-instance groups.
The main quality comparisons use common optimal sets of 84/47/21 instances;
extended comparisons use 62/83, and the objective-priority comparison uses 68.

Stress and Stress-high use generation seed 20260724 and precedence parameters
30/35/40 and 50/60, respectively. Their manifests record generation parameters
and witnesses. Larger precedence parameters retain the earlier arcs.

Runs used a 7,200-second limit and a c4-highcpu-8 resource profile (8 vCPUs,
16 GB RAM). UWrMaxSAT was invoked with `-m` and otherwise default parameters.
Recorded thread and seed fields describe the runner; solver commands have no
explicit thread or seed option. Recorded results retain commands, source
revisions, solver hashes, execution metadata, and reuse lineage. Solver builds
and software environments varied across runs; `analysis_report.json` reports
the comparisons restricted to matching metadata and their sample sizes.

OPT means at least one repetition proved optimality. Runtime and RSS first use
the median of optimal repetitions when available, otherwise all repetitions,
then the median across instances. PAR-2 assigns 14,400 seconds to timeouts,
takes the per-instance median, then averages instances. Paired runtime ratios
require all three repetitions to be optimal in both encodings.

Quality uses the lowest-numbered optimal repetition. Population standard
deviation describes participant idle totals within that selected schedule.
The comparisons are descriptive. `remaining_runs.csv` and `reuse_inventory.csv`
preserve historical planning records; the completed 6,102-run matrix is
documented in `analysis_report.json`.

`outputs/reproduction_manifest.json` records SHA-256 checksums for the research
files. The manuscript links to the specific repository commit used for submission.

## Citation

For this work, cite the article title and authors above. The shorter predecessor,
*Compact SAT and MaxSAT Encodings for Business-to-Business Meeting Scheduling
with Idle-Time Balancing*, by Long Duc Nguyen, Tuyen Van Kieu, and Khanh Van To,
was accepted for presentation at RIVF 2026. Its available preprint is
[arXiv:2608.00614](https://arxiv.org/abs/2608.00614).

Benchmarks and the 2022 formulation are described by
[Bofill et al., JAIR 74 (2022)](https://doi.org/10.1613/jair.1.12670).

## Licenses

The original code and data license notices are reproduced below. Benchmark
contents and third-party materials retain their source terms.

<details>
<summary>MIT License — source code</summary>

MIT License

Copyright (c) 2026 Nguyen.D Long

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

</details>

### Data license

Except where noted below, the author-generated experimental run records and
analysis outputs and stress-generation metadata in this repository are licensed under the Creative Commons
Attribution 4.0 International License (CC BY 4.0):
https://creativecommons.org/licenses/by/4.0/

This data license does not apply to:

- source code, which is distributed under the repository's MIT License;
- benchmark instance files obtained from the University of Girona B2B
  benchmark collection, including the underlying contents of derived Stress
  and Stress-high instances, which remain subject to their source terms; or
- third-party software, solver binaries, and other third-party materials.

When reusing the licensed records, please cite the accompanying article and
the archived artifact version from which the records were obtained.
