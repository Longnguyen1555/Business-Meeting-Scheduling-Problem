# Balanced waiting in B2B meeting scheduling

Research code for **Compact MaxSAT Encodings for Balanced Waiting in B2B Meeting
Scheduling**, by Tuyen Van Kieu, Long Duc Nguyen, Minh Duc Dang Vu, and Khanh Van To.

The study asks how to balance participants' waiting while keeping schedules
practical to compute. Minimizing the difference between the largest and smallest
idle totals can leave participants with long waits. The lexicographic objective
first minimizes this range, then maximum idle, and finally total idle. Balance
here concerns participants with at least two meetings; it does not normalize
waiting by meeting load.

The implementation separates two questions: how objective choice affects the
returned schedule, and how encoding choice affects formula size and solver
effort. **Compact-Base** provides the inherited compact feasibility encoding.
**Compact-Objective** extends it with objective-specific representations. The
comparison called **Direct** is an independent implementation based on the 2022
MaxSAT formulation, with the adaptations described in the manuscript. Clauses,
literal occurrences, and variables are reported separately: fewer clauses do
not imply that every size measure or runtime improves.

## Contents

| Path | Purpose |
| --- | --- |
| `src/` | Encodings, objectives, generators, experiment runner, and analysis |
| `tests/` | Model, runner, aggregation, and manuscript-audit tests |
| `journal_configs/` | Experiment definitions and configurations used by tests and launchers |
| `data_table*/`, `instances_manifest.csv` | Published B2B families and instance mapping |
| `data_precedence_stress*/` | Derived precedence instances and generation manifests |
| `outputs/cross_generation_analysis/` | Reference CSVs and analysis metadata |
| `scripts/` | Computational experiment launchers |
| [docs/reproduction.md](docs/reproduction.md) | Installation, analysis commands, and interpretation |

The reference CSVs cover encoding size, optimal and unsatisfiable outcomes,
runtime, PAR-2, memory, schedule quality, and ablations. The main matched study
contains 6,102 recorded runs: 226 instance variants, three encodings, three
objectives, and three repetitions. The variants originate from 20 base-instance
groups. Schedule comparisons use common optimal sets of 84, 47, and 21 instances;
extended comparisons use 62 and 83 instances.

## Reproduce the reported results

Use Python 3.10 or later. Analysis dependencies are pinned in
`src/requirements.txt`. Full reproduction uses the separate
**`JAIR_research_artifact_2026-11-17.zip`**, containing recorded runs, frozen
computational sources, manuscript sources, and all 29 reference CSVs. This
package is prepared locally and has not yet been published as a GitHub Release.
No permanent archive identifier has been assigned. The Git tree keeps 27 result
CSVs; two campaign-planning CSVs are supplied in the package.

Follow [the reproduction guide](docs/reproduction.md) from the extracted package.
Its main command, `python src/Reproduce_JAIR_Review.py`, rebuilds formula
diagnostics and summaries from recorded data without executing a solver. Paper
audits also use the manuscript directory included beside the code in that
package. New computational experiments require a compatible MaxSAT installation
and the hardware and configuration settings documented with the recorded runs.

## Citation and licenses

For the current work, cite the manuscript title and authors above. Its shorter
predecessor, *Compact SAT and MaxSAT Encodings for Business-to-Business Meeting
Scheduling with Idle-Time Balancing*, by Long Duc Nguyen, Tuyen Van Kieu, and
Khanh Van To, was accepted for presentation at RIVF 2026. The existing preprint is
[arXiv:2608.00614](https://arxiv.org/abs/2608.00614); a proceedings record was
unavailable when the manuscript was prepared.

The benchmark source and 2022 formulation are described by
[Bofill et al., JAIR 2022](https://doi.org/10.1613/jair.1.12670).
Code uses the [MIT license](LICENSE). Author-generated records and metadata use
CC BY 4.0, subject to the exclusions in [DATA_LICENSE.md](DATA_LICENSE.md).
Benchmark contents and third-party materials retain their source terms.
