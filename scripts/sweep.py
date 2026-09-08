#!/usr/bin/env python3
"""Parallel benchmark sweep over the table03/06/07/08 instance sets.

Work is queued per (solver, instance) so workers stay balanced to the end. Each
unit is one Main.py call covering both precedence modes for that instance and
writes its own CSV, so progress is visible as it happens and finished units are
skipped on re-run -- an interrupted sweep resumes by simply being restarted.
That also makes it safe on a preemptible/Spot VM: a reclaim costs at most the
in-flight units.

Examples
--------
    # everything, 3 workers (matches the local runs)
    python3 scripts/sweep.py --workers 3

    # one objective mode, one solver, on a big VM
    python3 scripts/sweep.py --objective lexicographic --solver incremental \\
        --workers 14 --timeout 3600 --tag lex3

    # regenerate the UNSAT skip list from an existing result CSV, then sweep
    python3 scripts/sweep.py --build-skip output/IncrementalSAT.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUTPUT = ROOT / "output"

TABLES = {
    "table03": "data_table03_origin",
    "table06": "data_table06_forb",
    "table07": "data_table07_fixed",
    "table08": "data_table08_prec",
}

# Main.py solver key -> stem of the delivered CSV.
SOLVER_CSV = {
    "incremental": "IncrementalSAT",
    "maxsat": "MaxSAT",
    "multiple": "MultipleSAT",
}

# Short suffix per objective, used for both the unit directory and the CSV name
# so runs of different objectives never share either.
OBJ_SUFFIX = {
    "idle-range": "range",
    "lexicographic": "lex3",
    "lex-idlesum": "sum",
}

SKIP_FILE = Path(__file__).with_name("unsat_skip.json")

_lock = threading.Lock()
_count = {"done": 0, "total": 0}


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


def build_skip(source_csv: Path) -> None:
    """Write the list of instances that are UNSAT under both precedence modes.

    Infeasibility depends only on the hard constraints, so it is unaffected by
    which objective is optimized and can be reused across sweeps.
    """
    by_instance: dict[tuple[str, str], dict[str, str]] = collections.defaultdict(dict)
    with source_csv.open() as handle:
        for row in csv.DictReader(handle):
            by_instance[(row["table"], row["instance"])][row["precedence_mode"]] = row["status"]
    unsat = sorted(
        f"{table}/{inst}"
        for (table, inst), modes in by_instance.items()
        if set(modes.values()) == {"UNSAT"}
    )
    SKIP_FILE.write_text(json.dumps(unsat, indent=0))
    print(f"wrote {SKIP_FILE.name}: {len(unsat)} instances skipped as UNSAT")


def load_skip() -> set[str]:
    if not SKIP_FILE.exists():
        return set()
    return set(json.loads(SKIP_FILE.read_text()))


def run_unit(args, objective: str, out: Path, solver: str,
             table: str, instance: Path) -> None:
    stem = instance.stem
    target = out / f"{solver}__{table}__{stem}_detailed.csv"
    if target.exists():
        with _lock:
            _count["done"] += 1
        return

    cmd = [
        sys.executable, "Main.py",
        "--instance", str(instance),
        "--solver", solver,
        "--precedence-mode", args.precedence,
        "--precedence-edges", "direct",
        "--objective-mode", objective,
        "--encoding-variant", args.variant,
        "--timeout", str(args.timeout),
        "--csv", str(out / f"{solver}__{table}__{stem}.csv"),
    ]
    started = time.time()
    proc = subprocess.run(cmd, cwd=SRC, capture_output=True, text=True)
    elapsed = time.time() - started

    with _lock:
        _count["done"] += 1
        done, total = _count["done"], _count["total"]

    if proc.returncode != 0 or not target.exists():
        log(f"[FAIL {done}/{total}] {solver}/{table}/{stem} "
            f"rc={proc.returncode} :: {proc.stderr.strip()[-300:]}")
        return
    with target.open() as handle:
        statuses = [r["status"] for r in csv.DictReader(handle)
                    if r.get("encoding_variant") == args.variant]
    log(f"[ok   {done}/{total}] {OBJ_SUFFIX[objective]}/{solver}/{table}/{stem} "
        f"{elapsed:.0f}s -> {','.join(statuses)}")


def merge(args, objective: str, out: Path, solver: str) -> None:
    rows: list[dict[str, str]] = []
    fields: list[str] = []
    for table in TABLES:
        prefix = f"{solver}__{table}__"
        for path in sorted(out.glob(f"{prefix}*_detailed.csv")):
            stem = path.name[len(prefix):-len("_detailed.csv")]
            with path.open() as handle:
                for row in csv.DictReader(handle):
                    if row.get("encoding_variant") != args.variant:
                        continue
                    # Older units predate the Main.py serializer fix and leave
                    # objective_value blank; idle_range carries the same value.
                    if not row.get("objective_value"):
                        row["objective_value"] = row.get("idle_range", "")
                    if not fields:
                        fields = ["table", "instance"] + list(row)
                    rows.append({"table": table, "instance": stem, **row})

    if not rows:
        log(f"[warn] {solver}: nothing to merge")
        return

    name = (SOLVER_CSV[solver] + "_" + OBJ_SUFFIX[objective]
            + (f"_{args.tag}" if args.tag else ""))
    target = OUTPUT / f"{name}.csv"
    OUTPUT.mkdir(exist_ok=True)
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    solved = sum(1 for r in rows if r["status"] == "OPTIMAL")
    errs = sum(1 for r in rows if r["validation_errors"] not in ("", "[]"))
    log(f"[csv ] {target.name} ({len(rows)} rows, {solved} OPTIMAL, {errs} errors)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build-skip", metavar="CSV",
                   help="regenerate the UNSAT skip list from this results CSV and exit")
    p.add_argument("--solver", default="all",
                   choices=[*SOLVER_CSV, "all"])
    p.add_argument("--objective", default=["lexicographic"], nargs="+",
                   choices=["idle-range", "lexicographic", "lex-idlesum"],
                   help="one or more objective modes; each gets its own unit "
                        "directory and its own set of output CSVs")
    p.add_argument("--variant", default="imp12+")
    p.add_argument("--precedence", default="both",
                   choices=["traditional", "staircase", "both"])
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--workers", type=int, default=3,
                   help="parallel Main.py processes; leave 1-2 cores free so "
                        "recorded runtimes stay comparable")
    p.add_argument("--tag", default="",
                   help="suffix for the output CSVs and the unit directory, so "
                        "runs of different objectives never share a directory")
    p.add_argument("--only-timeouts", metavar="CSV",
                   help="restrict to instances that TIMEOUT in this CSV")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.build_skip:
        build_skip(Path(args.build_skip))
        return

    skip = load_skip()
    restrict: set[tuple[str, str]] | None = None
    if args.only_timeouts:
        with Path(args.only_timeouts).open() as handle:
            restrict = {(r["table"], r["instance"])
                        for r in csv.DictReader(handle) if r["status"] == "TIMEOUT"}

    solvers = list(SOLVER_CSV) if args.solver == "all" else [args.solver]
    out_dirs = {}
    for objective in args.objective:
        name = "sweep_out_" + OBJ_SUFFIX[objective] + (f"_{args.tag}" if args.tag else "")
        out_dirs[objective] = SRC / name
        out_dirs[objective].mkdir(exist_ok=True)

    jobs = []
    skipped = 0
    for objective in args.objective:
        for solver in solvers:
            for table, data_dir in TABLES.items():
                for instance in sorted((ROOT / data_dir).glob("*.dzn")):
                    if f"{table}/{instance.stem}" in skip:
                        skipped += 1
                        continue
                    if restrict is not None and (table, instance.stem) not in restrict:
                        continue
                    jobs.append((objective, out_dirs[objective], solver, table, instance))

    _count["total"] = len(jobs)
    remaining = sum(
        1 for _o, o_dir, s, t, i in jobs
        if not (o_dir / f"{s}__{t}__{i.stem}_detailed.csv").exists()
    )
    log(f"[start] objectives={','.join(args.objective)} variant={args.variant} "
        f"precedence={args.precedence} timeout={args.timeout}s "
        f"workers={args.workers}")
    log(f"[start] {len(jobs)} units ({remaining} still to run), "
        f"{skipped} skipped as known-UNSAT")

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_unit, args, job[0], job[1], *job[2:])
                   for job in jobs]
        for future in as_completed(futures):
            future.result()

    for objective in args.objective:
        for solver in solvers:
            merge(args, objective, out_dirs[objective], solver)
    log(f"SWEEP COMPLETE in {(time.time() - started) / 3600:.2f}h")


if __name__ == "__main__":
    main()
