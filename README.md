# Balanced waiting in B2B meeting scheduling

Code, benchmarks, and results for **Compact MaxSAT Encodings for Balanced Waiting
in B2B Meeting Scheduling**, by Tuyen Van Kieu, Long Duc Nguyen, Minh Duc Dang Vu,
and Khanh Van To.

Minimizing the range of participant idle totals can leave long waits. The study
refines balance lexicographically: minimize **idle range, then maximum idle, then
total idle**. Balance is measured over participants with at least two meetings.
Compact-Base supplies the inherited compact feasibility encoding;
Compact-Objective extends it with objective-specific representations. Direct is
an independent implementation based on the 2022 MaxSAT formulation, with the
adaptations described in the manuscript. Variables, clauses, literal occurrences,
schedule quality, and solver effort are evaluated separately.

## Repository contents

| Location | Contents |
| --- | --- |
| `src/` | Encodings, objectives, generators, experiment runner, analysis, dependencies |
| `journal_configs/` | 23 configurations and shared definitions used by the reported studies |
| `scripts/run_matched_encoding_three_vms.sh` | Launcher for the frozen matched campaign |
| `data_table*/`, `instances_manifest.csv` | Published benchmark families and instance mapping |
| `data_precedence_stress*/` | Derived instances, manifests, generation metadata, witness schedules |
| `outputs/cross_generation_analysis/` | 27 reference CSVs and two analysis metadata files |

The matched study has **6,102 recorded runs**: 226 variants × three encodings ×
three objectives × three repetitions. The variants share 20 base-instance groups.
Main schedule-quality comparisons use common optimal sets of **84/47/21**
instances; extended comparisons use **62/83**.

## Installation

Use Python 3.10 or later. From a Git checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
python src/Main.py --help
```

Dependencies are pinned to `python-sat==1.8.dev25` and `psutil==7.2.2`.
Configurations record experiment options and hardware requirements. New solver
runs need the solver binaries and environment settings specified there. The
matched launcher additionally uses the frozen computational bundle supplied in
the separate research package.

## Reproduce the manuscript results

Full reproduction uses **`JAIR_research_artifact_2026-11-17.zip`**, prepared
locally as a separate research package. It has not yet been published as a
GitHub Release and has no permanent archive identifier. It contains recorded
runs, frozen computational sources, all 29 reference CSVs, tests, and manuscript
sources. The Git checkout contains the code, inputs, and selected summaries;
two historical campaign-planning CSVs are supplied in the package.

Keep the extracted package layout, with `Business-Meeting-Scheduling-Problem/`,
`JAIR_Example_Template/`, and `manuscript/` as sibling directories. From that
package root, use a Python environment with the pinned dependencies:

```bash
python3 verify_snapshot.py
cd Business-Meeting-Scheduling-Problem
python -m pip install -r src/requirements.txt
tar -xzf outputs/journal/matched-encoding-v2-results/matched-encoding-v2-complete.tar.gz -C outputs/journal
python src/Reproduce_JAIR_Review.py
```

This reconstructs formula diagnostics and summaries without running a solver.
It checks all 29 CSVs and the generated LaTeX rows against their references.
Outputs go to `outputs/reproduced_review/`; use `--output-dir` to choose another
directory. `--skip-formula-rebuild` gives a quicker check of the recorded results.

<details>
<summary>Compile and audit the paper from the extracted package</summary>

After the default reproduction, with TeX Live, Biber, latexmk, TikZ, and PGFPlots
installed, run these commands from the package's code directory:

```bash
cd ../JAIR_Example_Template
python3 figures/prepare_tikz_data.py --check
latexmk -pdf -interaction=nonstopmode -halt-on-error JAIR_Example_Template.tex
cd ../Business-Meeting-Scheduling-Problem
python src/Audit_JAIR_Update.py --analysis outputs/reproduced_review --report outputs/reproduced_review/manuscript_audit.json
python -m unittest discover -s tests -p test_audit_jair_update.py -v
```

The audit checks the main tables, four appendix result tables, highlighting,
and figure data. The package retains the 12 audit tests. Use the full formula
reconstruction before this audit. The prepared package was verified with
Python 3.12 and TeX Live 2025.

</details>

## Reading the data and results

All-126, Stress, and Stress-high contain 126, 60, and 40 variants. The two derived
sets use precedence densities 30/35/40 and 50/60, respectively, on 20 base
instances. Their generation seed is 20260724. Higher densities retain the edges
of lower densities; witness schedules and realized densities are recorded in
the CSVs. `metadata.json` records dataset parameters, and the generator is
`src/Generate_Precedence_Stress.py`.

An instance is OPT when any repetition proves optimality; solved includes OPT
and proven UNSAT. Runtime and RSS use the median of OPTIMAL repetitions for an
OPT instance, otherwise all three, followed by medians over instances. PAR-2
assigns 14,400 seconds to each timeout, takes the per-instance median, then
averages instances. Paired runtime ratios require three OPTIMAL repetitions in
both encodings.

Quality selects the lowest-numbered OPTIMAL repetition. Population standard
deviation describes participant idle totals within that selected schedule; it
is not an optimization objective or variability between repetitions. Results
describe the benchmark family and selected schedules, rather than all optimal
schedules. The recorded runs preserve solver commands, hashes, environments,
and reuse lineage in the separate package.

## Citation

For the current work, cite the manuscript title and authors above. Its shorter
predecessor, *Compact SAT and MaxSAT Encodings for Business-to-Business Meeting
Scheduling with Idle-Time Balancing*, by Long Duc Nguyen, Tuyen Van Kieu, and
Khanh Van To, was accepted for presentation at RIVF 2026. The available preprint
is [arXiv:2608.00614](https://arxiv.org/abs/2608.00614); its proceedings record
was unavailable when the manuscript was prepared.

Benchmarks and the 2022 formulation are described by
[Bofill et al., JAIR 2022](https://doi.org/10.1613/jair.1.12670).

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
