from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

import IncrementalSAT_Solver
import Multiple_SAT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run B2B SAT solvers and save timed results to CSV."
    )
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Path to a single .dzn instance. If omitted, all .dzn files in --data-dir are used.",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
        help="Directory containing .dzn instances when --instance is not given.",
    )
    parser.add_argument(
        "--solver",
        choices=["incremental", "multiple", "all"],
        default="all",
        help="Which SAT solver to run.",
    )
    parser.add_argument(
        "--precedence-mode",
        choices=["traditional", "staircase", "both"],
        default="both",
        help="Which precedence encoding to run.",
    )
    parser.add_argument(
        "--fairness",
        type=int,
        default=2,
        help="Fairness difference bound d. Use -1 to disable fairness constraints.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Wall-clock timeout in seconds per solver run.",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="summary.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print solver progress inside each worker process.",
    )
    return parser.parse_args()


def normalize_fairness(raw_fairness: int) -> int | None:
    return None if raw_fairness < 0 else raw_fairness


def collect_instances(single_instance: str | None, data_dir: str) -> list[Path]:
    if single_instance is not None:
        path = Path(single_instance)
        if not path.is_file():
            raise FileNotFoundError(f"Instance file not found: {path}")
        return [path]

    folder = Path(data_dir)
    if not folder.is_dir():
        raise FileNotFoundError(f"Data directory not found: {folder}")

    instances = sorted(folder.glob("*.dzn"))
    if not instances:
        raise FileNotFoundError(f"No .dzn files found in: {folder}")
    return instances


def serialize_assignment(assignment: list[int] | None) -> str:
    if assignment is None:
        return ""
    return ",".join(str(slot + 1) for slot in assignment)


def serialize_int_list(values: list[int] | None) -> str:
    if values is None:
        return ""
    return ",".join(str(v) for v in values)


def serialize_schedule_by_slot(meetings_per_slot: list[list[int]] | None) -> str:
    if meetings_per_slot is None:
        return ""
    parts: list[str] = []
    for t, meetings in enumerate(meetings_per_slot, start=1):
        payload = " ".join(f"M{m + 1}" for m in meetings)
        parts.append(f"{t}:{payload}")
    return " | ".join(parts)


def _worker(
    solver_name: str,
    instance_path: str,
    fairness_limit: int | None,
    precedence_mode: str,
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
                verbose=verbose,
            )
        elif solver_name == "multiple":
            result = Multiple_SAT.solve_b2b(
                instance_or_path=instance_path,
                fairness_limit=fairness_limit,
                precedence_mode=precedence_mode,
                verbose=verbose,
            )
        else:
            raise ValueError(f"Unknown solver: {solver_name}")

        stats = result.get("stats")
        queue.put(
            {
                "status": result.get("status", "ERROR"),
                "solver": result.get("solver", solver_name),
                "precedence_mode": result.get("precedence_mode", precedence_mode),
                "runtime_s": round(time.perf_counter() - started, 6),
                "n_vars": result.get("n_vars"),
                "n_clauses": result.get("n_clauses"),
                "assignment": serialize_assignment(result.get("assignment")),
                "total_breaks": None if stats is None else stats.total_breaks,
                "fairness_gap": None if stats is None else stats.fairness_gap,
                "participant_breaks": None if stats is None else serialize_int_list(stats.participant_breaks),
                "busy_per_slot": None if stats is None else serialize_int_list(stats.busy_per_slot),
                "schedule_by_slot": None if stats is None else serialize_schedule_by_slot(stats.meetings_per_slot),
                "error_type": "",
                "error_message": "",
            }
        )
    except Exception as exc:
        queue.put(
            {
                "status": "ERROR",
                "solver": solver_name,
                "precedence_mode": precedence_mode,
                "runtime_s": round(time.perf_counter() - started, 6),
                "n_vars": None,
                "n_clauses": None,
                "assignment": "",
                "total_breaks": None,
                "fairness_gap": None,
                "participant_breaks": "",
                "busy_per_slot": "",
                "schedule_by_slot": "",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        )


def run_with_timeout(
    solver_name: str,
    instance_path: Path,
    fairness_limit: int | None,
    precedence_mode: str,
    timeout_s: int,
    verbose: bool,
) -> dict[str, Any]:
    queue: mp.Queue = mp.Queue()
    process = mp.Process(
        target=_worker,
        args=(solver_name, str(instance_path), fairness_limit, precedence_mode, verbose, queue),
    )

    started = time.perf_counter()
    process.start()
    process.join(timeout_s)

    if process.is_alive():
        process.terminate()
        process.join()
        return {
            "status": "TIMEOUT",
            "solver": "IncrementalSAT" if solver_name == "incremental" else "MultipleSAT",
            "precedence_mode": precedence_mode,
            "runtime_s": round(time.perf_counter() - started, 6),
            "n_vars": None,
            "n_clauses": None,
            "assignment": "",
            "total_breaks": None,
            "fairness_gap": None,
            "participant_breaks": "",
            "busy_per_slot": "",
            "schedule_by_slot": "",
            "error_type": "",
            "error_message": "",
        }

    if queue.empty():
        return {
            "status": "ERROR",
            "solver": "IncrementalSAT" if solver_name == "incremental" else "MultipleSAT",
            "precedence_mode": precedence_mode,
            "runtime_s": round(time.perf_counter() - started, 6),
            "n_vars": None,
            "n_clauses": None,
            "assignment": "",
            "total_breaks": None,
            "fairness_gap": None,
            "participant_breaks": "",
            "busy_per_slot": "",
            "schedule_by_slot": "",
            "error_type": "NoWorkerPayload",
            "error_message": "Worker process ended without returning a payload.",
        }

    return queue.get()


def build_solver_list(choice: str) -> list[str]:
    if choice == "all":
        return ["incremental", "multiple"]
    return [choice]


def build_precedence_list(choice: str) -> list[str]:
    if choice == "both":
        return ["traditional", "staircase"]
    return [choice]


def main() -> None:
    args = parse_args()
    fairness_limit = normalize_fairness(args.fairness)
    instances = collect_instances(args.instance, args.data_dir)
    solver_names = build_solver_list(args.solver)
    precedence_modes = build_precedence_list(args.precedence_mode)

    rows: list[dict[str, Any]] = []

    for instance_path in instances:
        for solver_name in solver_names:
            for precedence_mode in precedence_modes:
                print(
                    f"Running {instance_path.name} | {solver_name} | {precedence_mode} | timeout={args.timeout}s"
                )
                result = run_with_timeout(
                    solver_name=solver_name,
                    instance_path=instance_path,
                    fairness_limit=fairness_limit,
                    precedence_mode=precedence_mode,
                    timeout_s=args.timeout,
                    verbose=args.verbose,
                )

                rows.append(
                    {
                        "instance": instance_path.name,
                        "solver": result["solver"],
                        "precedence_mode": result["precedence_mode"],
                        "fairness_limit": "none" if fairness_limit is None else fairness_limit,
                        "timeout_s": args.timeout,
                        "status": result["status"],
                        "runtime_s": result["runtime_s"],
                        "total_breaks": result["total_breaks"],
                        "fairness_gap": result["fairness_gap"],
                        "n_vars": result["n_vars"],
                        "n_clauses": result["n_clauses"],
                        "participant_breaks": result["participant_breaks"],
                        "busy_per_slot": result["busy_per_slot"],
                        "assignment": result["assignment"],
                        "schedule_by_slot": result["schedule_by_slot"],
                        "error_type": result["error_type"],
                        "error_message": result["error_message"],
                    }
                )

    csv_path = Path(args.csv)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "instance",
                "solver",
                "precedence_mode",
                "fairness_limit",
                "timeout_s",
                "status",
                "runtime_s",
                "total_breaks",
                "fairness_gap",
                "n_vars",
                "n_clauses",
                "participant_breaks",
                "busy_per_slot",
                "assignment",
                "schedule_by_slot",
                "error_type",
                "error_message",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved CSV to {csv_path}")


if __name__ == "__main__":
    main()
