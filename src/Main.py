from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

import IncrementalSAT_Solver
import Multiple_SAT

# =========================================================
# CONFIG
# =========================================================

VARIANTS = [
    "basic",
    "imp1",
    "imp2",
    "imp12",
    "imp12+",
]

SOLVERS = [
    "incremental",
    "multiple",
]

PRECEDENCE_MODES = [
    "traditional",
    "staircase",
]


# =========================================================
# ARGUMENTS
# =========================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description="B2B SAT Benchmark Runner"
    )

    parser.add_argument(
        "--instance",
        default=None,
        help="Single .dzn instance"
    )

    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing .dzn instances"
    )

    parser.add_argument(
        "--solver",
        choices=["incremental", "multiple", "all"],
        default="all",
    )

    parser.add_argument(
        "--precedence-mode",
        choices=["traditional", "staircase", "both"],
        default="both",
    )

    parser.add_argument(
        "--encoding-variant",
        choices=VARIANTS + ["all"],
        default="all",
    )

    parser.add_argument(
        "--fairness",
        type=int,
        default=2,
        help="Fairness bound d. Use -1 to disable fairness."
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Timeout per run in seconds"
    )

    parser.add_argument(
        "--csv",
        default="table3_results.csv",
        help="Output CSV path"
    )

    parser.add_argument(
        "--long-csv",
        default=None,
        help="Optional detailed CSV"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
    )

    return parser.parse_args()


# =========================================================
# HELPERS
# =========================================================

def normalize_fairness(value: int) -> int | None:

    if value < 0:
        return None

    return value


def selected_solvers(choice: str) -> list[str]:

    if choice == "all":
        return SOLVERS

    return [choice]


def selected_precedence_modes(choice: str) -> list[str]:

    if choice == "both":
        return PRECEDENCE_MODES

    return [choice]


def selected_variants(choice: str) -> list[str]:

    if choice == "all":
        return VARIANTS

    return [choice]


def collect_instances(
    instance: str | None,
    data_dir: str,
) -> list[Path]:

    if instance:

        path = Path(instance)

        if not path.is_file():
            raise FileNotFoundError(path)

        return [path]

    folder = Path(data_dir)

    if not folder.is_dir():
        raise FileNotFoundError(folder)

    files = sorted(folder.glob("*.dzn"))

    if not files:
        raise FileNotFoundError(
            f"No .dzn files found in {folder}"
        )

    return files


# =========================================================
# SERIALIZERS
# =========================================================

def serialize_list(values: list[int] | None) -> str:

    if values is None:
        return ""

    return ",".join(str(v) for v in values)


def serialize_assignment(values: list[int] | None) -> str:

    if values is None:
        return ""

    return ",".join(
        str(v + 1) if v >= 0 else "-"
        for v in values
    )


def serialize_schedule(
    meetings_per_slot: list[list[int]] | None
) -> str:

    if meetings_per_slot is None:
        return ""

    parts = []

    for slot, meetings in enumerate(
        meetings_per_slot,
        start=1
    ):

        text = " ".join(
            f"M{m + 1}"
            for m in meetings
        )

        parts.append(f"{slot}:{text}")

    return " | ".join(parts)


# =========================================================
# WORKER
# =========================================================

def _worker(
    solver_name: str,
    instance_path: str,
    fairness_limit: int | None,
    precedence_mode: str,
    encoding_variant: str,
    verbose: bool,
    queue: mp.Queue,
) -> None:

    started = time.perf_counter()

    try:

        if solver_name == "incremental":

            result = IncrementalSAT_Solver.solve_b2b(
                instance_or_path=instance_path,
                fairness_limit=fairness_limit,
                precedence_mode=precedence_mode,
                encoding_variant=encoding_variant,
                verbose=verbose,
            )

        elif solver_name == "multiple":

            result = Multiple_SAT.solve_b2b(
                instance_or_path=instance_path,
                fairness_limit=fairness_limit,
                precedence_mode=precedence_mode,
                encoding_variant=encoding_variant,
                verbose=verbose,
            )

        else:

            raise ValueError(
                f"Unknown solver: {solver_name}"
            )

        stats = result.get("stats")

        queue.put({

            "status":
                result.get("status", "ERROR"),

            "solver":
                solver_name,

            "precedence_mode":
                precedence_mode,

            "encoding_variant":
                encoding_variant,

            "runtime_s":
                round(
                    time.perf_counter() - started,
                    6
                ),

            "total_breaks":
                None if stats is None
                else stats.total_breaks,

            "fairness_gap":
                None if stats is None
                else stats.fairness_gap,

            "participant_breaks":
                "" if stats is None
                else serialize_list(
                    stats.participant_breaks
                ),

            "busy_participants_per_slot":
                "" if stats is None
                else serialize_list(
                    stats.busy_participants_per_slot
                ),

            "assignment":
                serialize_assignment(
                    result.get("assignment")
                ),

            "schedule_by_slot":
                "" if stats is None
                else serialize_schedule(
                    stats.meetings_per_slot
                ),

            "validation_errors":
                "; ".join(
                    result.get(
                        "validation_errors",
                        []
                    )
                ),

            "n_vars":
                result.get("n_vars"),

            "n_clauses":
                result.get("n_clauses"),

            "enabled_constraints":
                " | ".join(
                    result.get(
                        "enabled_constraints",
                        []
                    )
                ),

            "error_type": "",

            "error_message": "",
        })

    except Exception as exc:

        queue.put({

            "status": "ERROR",

            "solver": solver_name,

            "precedence_mode":
                precedence_mode,

            "encoding_variant":
                encoding_variant,

            "runtime_s":
                round(
                    time.perf_counter() - started,
                    6
                ),

            "total_breaks": None,

            "fairness_gap": None,

            "participant_breaks": "",

            "busy_participants_per_slot": "",

            "assignment": "",

            "schedule_by_slot": "",

            "validation_errors": "",

            "n_vars": None,

            "n_clauses": None,

            "enabled_constraints": "",

            "error_type":
                type(exc).__name__,

            "error_message":
                str(exc),
        })


# =========================================================
# TIMEOUT WRAPPER
# =========================================================

def run_with_timeout(
    solver_name: str,
    instance_path: Path,
    fairness_limit: int | None,
    precedence_mode: str,
    encoding_variant: str,
    timeout_s: int,
    verbose: bool,
) -> dict[str, Any]:

    queue: mp.Queue = mp.Queue()

    proc = mp.Process(
        target=_worker,
        args=(
            solver_name,
            str(instance_path),
            fairness_limit,
            precedence_mode,
            encoding_variant,
            verbose,
            queue,
        )
    )

    started = time.perf_counter()

    proc.start()

    proc.join(timeout_s)

    if proc.is_alive():

        proc.terminate()
        proc.join()

        return {

            "status": "TIMEOUT",

            "solver": solver_name,

            "precedence_mode":
                precedence_mode,

            "encoding_variant":
                encoding_variant,

            "runtime_s":
                round(
                    time.perf_counter() - started,
                    6
                ),

            "total_breaks": None,
        }

    if queue.empty():

        return {

            "status": "ERROR",

            "solver": solver_name,

            "precedence_mode":
                precedence_mode,

            "encoding_variant":
                encoding_variant,

            "runtime_s":
                round(
                    time.perf_counter() - started,
                    6
                ),

            "total_breaks": None,

            "error_type":
                "NoWorkerPayload",

            "error_message":
                "Worker returned nothing",
        }

    return queue.get()


# =========================================================
# TABLE 3 FORMAT
# =========================================================

def format_table3_cell(
    result: dict[str, Any]
) -> str:

    status = result.get("status")

    runtime = result.get("runtime_s")

    breaks = result.get("total_breaks")

    if status == "TIMEOUT":

        return f"TO {breaks if breaks is not None else '-'}"

    if status == "OPTIMAL":

        if runtime is None:
            return f"? {breaks}"

        return f"{runtime:.1f} {breaks}"

    return "ERR"


# =========================================================
# MAIN
# =========================================================

def main():

    args = parse_args()

    fairness_limit = normalize_fairness(
        args.fairness
    )

    instances = collect_instances(
        args.instance,
        args.data_dir,
    )

    solver_list = selected_solvers(
        args.solver
    )

    precedence_modes = selected_precedence_modes(
        args.precedence_mode
    )

    variants = selected_variants(
        args.encoding_variant
    )

    results = []

    total_runs = (
        len(instances)
        * len(solver_list)
        * len(precedence_modes)
        * len(variants)
    )

    current_run = 0

    print("\n" + "=" * 120)
    print("B2B SAT BENCHMARK")
    print("=" * 120)

    for instance_path in instances:

        instance_name = instance_path.stem

        for precedence_mode in precedence_modes:

            staircase = (
                "yes"
                if precedence_mode == "staircase"
                else "no"
            )

            for solver_name in solver_list:

                for variant in variants:

                    current_run += 1

                    print(
                        f"\n[{current_run}/{total_runs}] "
                        f"{instance_name} | "
                        f"{solver_name} | "
                        f"{precedence_mode} | "
                        f"{variant}"
                    )

                    result = run_with_timeout(
                        solver_name=solver_name,
                        instance_path=instance_path,
                        fairness_limit=fairness_limit,
                        precedence_mode=precedence_mode,
                        encoding_variant=variant,
                        timeout_s=args.timeout,
                        verbose=args.verbose,
                    )

                    row = {

                        "instance":
                            instance_name,

                        "staircase":
                            staircase,

                        "solver":
                            solver_name,

                        "precedence_mode":
                            precedence_mode,

                        "encoding_variant":
                            variant,

                        **result,
                    }

                    results.append(row)

                    print(
                        f"   status={result.get('status')} | "
                        f"time={result.get('runtime_s')} | "
                        f"breaks={result.get('total_breaks')}"
                    )

    # =====================================================
    # AGGREGATE TABLE 3
    # =====================================================

    grouped = {}

    for r in results:

        key = (
            r["instance"],
            r["staircase"],
            r["solver"],
        )

        if key not in grouped:

            grouped[key] = {

                "instance":
                    r["instance"],

                "staircase":
                    r["staircase"],

                "solver":
                    r["solver"],
            }

        grouped[key][
            r["encoding_variant"]
        ] = format_table3_cell(r)

    table3_rows = []

    for row in grouped.values():

        for variant in VARIANTS:

            if variant not in row:
                row[variant] = "-"

        table3_rows.append(row)

    table3_rows.sort(
        key=lambda x: (
            x["instance"],
            x["solver"],
            x["staircase"],
        )
    )

    # =====================================================
    # PRINT TABLE 3
    # =====================================================

    print("\n")
    print("=" * 150)

    header = (
        f"{'instance':<30}"
        f"{'stairs':<10}"
        f"{'solver':<15}"
    )

    for variant in VARIANTS:
        header += f"{variant:<18}"

    print(header)

    print("=" * 150)

    for row in table3_rows:

        line = (
            f"{row['instance']:<30}"
            f"{row['staircase']:<10}"
            f"{row['solver']:<15}"
        )

        for variant in VARIANTS:

            line += (
                f"{row[variant]:<18}"
            )

        print(line)

    print("=" * 150)

    # =====================================================
    # EXPORT TABLE 3 CSV
    # =====================================================

    csv_fields = [

        "instance",

        "staircase",

        "solver",

        *VARIANTS,
    ]

    with open(
        args.csv,
        "w",
        newline=""
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=csv_fields,
        )

        writer.writeheader()

        for row in table3_rows:
            writer.writerow(row)

    print(
        f"\nTable-3 CSV exported to: "
        f"{args.csv}"
    )

    # =====================================================
    # OPTIONAL LONG CSV
    # =====================================================

    if args.long_csv:

        with open(
            args.long_csv,
            "w",
            newline=""
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=results[0].keys()
            )

            writer.writeheader()

            for row in results:
                writer.writerow(row)

        print(
            f"Long CSV exported to: "
            f"{args.long_csv}"
        )

    # =====================================================
    # SUMMARY
    # =====================================================

    print("\nSUMMARY")

    for solver_name in solver_list:

        solved = sum(
            1
            for r in results
            if r["solver"] == solver_name
            and r["status"] == "OPTIMAL"
        )

        timeout = sum(
            1
            for r in results
            if r["solver"] == solver_name
            and r["status"] == "TIMEOUT"
        )

        errors = sum(
            1
            for r in results
            if r["solver"] == solver_name
            and r["status"] == "ERROR"
        )

        print(
            f"{solver_name:<15}"
            f" solved={solved:<5}"
            f" timeout={timeout:<5}"
            f" error={errors:<5}"
        )

    print("\nDone.")


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    main()