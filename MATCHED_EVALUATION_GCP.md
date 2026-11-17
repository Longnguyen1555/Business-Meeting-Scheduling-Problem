# Matched encoding evaluation

The campaign is complete: 6,102 recorded runs cover 226 variants, three encodings,
three objectives, and three repetitions. All-126, Stress, and Stress-high
contribute 3,402, 1,620, and 1,080 runs. Recorded outcomes are 4,577 OPTIMAL,
1,017 UNSAT, and 508 TIMEOUT.

## Reconstructing the results

Use the separate research artifact and [reproduction guide](docs/reproduction.md).
The analysis starts from recorded data without running a solver. Complete raw
records, normalized CSVs, schedules, environments, and validation reports are
distributed in `matched-encoding-v2-complete.tar.gz` inside that package.
Earlier planning files describe preparation, rather than outstanding runs.

## Computational configuration

The design is in `journal_configs/matched_encoding_evaluation_v2.json` and its
referenced files. Runs used a 7,200-second wall-clock cutoff and the common
c4-highcpu-8 resource profile (8 vCPUs, 16 GB RAM). UWrMaxSAT was invoked with
`-m` and otherwise default solver parameters. The runner recorded `threads=1`
and `random_seed=0`; recorded solver commands contain no explicit thread or
seed option.

Component campaigns used seven solver-binary hashes and Python versions 3.10.12
and 3.12.3. The final completion stage's UWrMaxSAT SHA-256 is
`29c51abeabd43015c75a8e6ee789fdbb9aa2803096d2cac536ec60bd516ab4a7`.
Records retain actual commands, hashes, environments, and reuse lineage.
Original absolute paths describe the machines used for those runs.

## Running new experiments

The research package preserves `outputs/matched-encoding-v2-gcp.tar.gz`, the
computational source and reuse bundle used to complete the campaign. Later
analysis and documentation edits are separate from that source snapshot.
Existing shell launchers use this bundle, `journal_gcp.env.example`, and
`journal_configs/`. They require the specified solver and environment pins and
can create new computational runs. Consult their command-line help and
configuration files before using them; the recorded-data reproduction does not
need these launchers.
