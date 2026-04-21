# B2B SAT Project (refactored)

This refactored folder follows the structure requested for the B2B SAT project:

- `Main.py`
  - only orchestrates solver calls
  - applies a wall-clock timeout per run
  - writes one CSV summary file
- `B2B_Instance.py`
  - reads `.dzn` instances
  - creates SAT variables
  - contains all shared constraints used by both SAT solvers
  - supports two precedence encodings:
    - `traditional`
    - `staircase`
- `IncrementalSAT_Solver.py`
  - imports the shared model from `B2B_Instance.py`
  - solves by tightening the total-break bound incrementally
- `Multiple_SAT.py`
  - imports the shared model from `B2B_Instance.py`
  - rebuilds the SAT solver for each objective bound
- `requirements.txt`
  - Python dependency for PySAT
- `data/`
  - sample `.dzn` instances copied into the project

## Run examples

Run all solvers and both precedence variants on one instance:

```bash
python Main.py \
  --instance data/forum-13.original.dzn \
  --solver all \
  --precedence-mode both \
  --fairness 2 \
  --timeout 120 \
  --csv summary.csv
```

Run only Incremental SAT with staircase precedence:

```bash
python Main.py \
  --instance data/forum-13.prec15.dzn \
  --solver incremental \
  --precedence-mode staircase \
  --fairness 2 \
  --timeout 120 \
  --csv staircase_only.csv
```

Run only Multiple SAT with traditional precedence on all data files:

```bash
python Main.py \
  --data-dir data \
  --solver multiple \
  --precedence-mode traditional \
  --fairness 2 \
  --timeout 120 \
  --csv multiple_traditional.csv
```


## Notes on the encoding

- `x(m,t)`: meeting `m` is scheduled at slot `t`
- `used(p,t)`: participant `p` has a meeting at slot `t`
- `held(p,t)`: participant `p` has already had a meeting by slot `t`
- `hole(p,t)`: a break for participant `p` finishes at slot `t`

