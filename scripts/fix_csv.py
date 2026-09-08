#!/usr/bin/env python3
"""Rewrite result CSVs to match the deduplicated instance directories.

The raw instance set contained 54 redundant files: data_table07_fixed was a
byte-identical copy of 40 table06 files, and table06 held 14 internal duplicate
pairs. After removing them and moving the *fixed* instances into table07, any
CSV produced beforehand carries stale rows and stale table labels.

Three corrections, in order:
  1. Relabel the ``table`` column from the instance name -- *fixed* lives in
     table07, *forb* in table06.
  2. Drop rows whose instance no longer exists on disk.
  3. Collapse duplicate rows. The two copies were identical inputs, so their
     rows should agree; any that disagree are reported rather than discarded.

Run after any sweep whose unit directory predates the deduplication:
    python3 scripts/fix_csv.py output/IncrementalSAT_lex3.csv
    python3 scripts/fix_csv.py            # all standard result CSVs
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

DIRS = {
    "table03": "data_table03_origin",
    "table06": "data_table06_forb",
    "table07": "data_table07_fixed",
    "table08": "data_table08_prec",
}

DEFAULT_FILES = [
    f"{solver}{suffix}"
    for solver in ("IncrementalSAT", "MaxSAT", "MultipleSAT")
    for suffix in ("", "_lex3", "_sum", "_lex3_min")
]

# Runtime and memory legitimately differ between two solves of the same input;
# these are the columns that must agree for two rows to be "the same result".
COMPARE = [
    "status", "objective_value", "secondary_objective_value",
    "tertiary_objective_value", "total_breaks", "idle_range",
]


def on_disk() -> dict[str, set[str]]:
    return {t: {p.stem for p in (ROOT / d).glob("*.dzn")} for t, d in DIRS.items()}


def correct_table(instance: str, current: str) -> str:
    if "fixed" in instance:
        return "table07"
    if "forb" in instance:
        return "table06"
    return current


def fix(path: Path, valid: dict[str, set[str]]) -> None:
    if not path.exists():
        print(f"  {path.name}: missing, skipped")
        return

    with path.open() as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    before = len(rows)
    kept: dict[tuple[str, str, str], dict[str, str]] = {}
    dropped = collapsed = 0
    conflicts: list[str] = []

    for row in rows:
        inst = row["instance"]
        row["table"] = correct_table(inst, row["table"])
        if inst not in valid.get(row["table"], set()):
            dropped += 1
            continue
        key = (row["table"], inst, row["precedence_mode"])
        if key in kept:
            collapsed += 1
            prior = kept[key]
            diff = [c for c in COMPARE if prior.get(c) != row.get(c)]
            if diff:
                conflicts.append(f"{'/'.join(key)}: " + ", ".join(
                    f"{c} {prior.get(c)!r} vs {row.get(c)!r}" for c in diff))
            continue
        kept[key] = row

    ordered = sorted(kept.values(),
                     key=lambda r: (r["table"], r["instance"], r["precedence_mode"]))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)

    instances = len({(r["table"], r["instance"]) for r in ordered})
    print(f"  {path.name:26s} {before:4d} -> {len(ordered):4d} rows "
          f"({instances} instances)  dropped={dropped} collapsed={collapsed} "
          f"conflicts={len(conflicts)}")
    for c in conflicts[:5]:
        print(f"      CONFLICT {c}")


def main() -> None:
    valid = on_disk()
    print("instances on disk:", {t: len(v) for t, v in valid.items()})
    targets = ([Path(a) for a in sys.argv[1:]]
               if len(sys.argv) > 1
               else [OUTPUT / f"{n}.csv" for n in DEFAULT_FILES])
    for path in targets:
        fix(path, valid)


if __name__ == "__main__":
    main()
