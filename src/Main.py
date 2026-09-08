from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import queue as queue_module
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # The benchmark still runs, but memory will be reported as N/A.
    psutil = None

import IncrementalSAT_Solver
import MaxSAT_Solver
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
    "maxsat",
]

PRECEDENCE_MODES = [
    "traditional",
    "staircase",
]

PRECEDENCE_EDGE_MODES = [
    "direct",
    "source-closure",
]

OBJECTIVE_MODES = [
    "idle-range",
    "lexicographic",
    "lex-idlesum",
]

MEMORY_SAMPLE_INTERVAL_S = 0.05
QUEUE_RESULT_GRACE_S = 1.0


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
        help="Single .dzn instance",
    )

    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing .dzn instances",
    )

    parser.add_argument(
        "--solver",
        choices=["incremental", "multiple", "maxsat", "all"],
        default="all",
    )

    parser.add_argument(
        "--precedence-mode",
        choices=["traditional", "staircase", "both"],
        default="both",
    )

    parser.add_argument(
        "--precedence-edges",
        choices=[*PRECEDENCE_EDGE_MODES, "both"],
        default="direct",
        help=(
            "Use direct precedence edges E, or E_R=E plus reachability "
            "shortcuts whose first endpoint is a source vertex."
        ),
    )

    parser.add_argument(
        "--objective-mode",
        choices=[*OBJECTIVE_MODES, "both"],
        default="lexicographic",
        help=(
            "idle-range: IdleRange(P*) only. lexicographic: "
            "(IdleRange, IdleMax, IdleSum). lex-idlesum: (IdleRange, IdleSum), "
            "dropping the IdleMax level."
        ),
    )

    parser.add_argument(
        "--encoding-variant",
        choices=VARIANTS + ["all"],
        default="all",
    )

    parser.add_argument(
        "--fairness",
        type=int,
        default=-1,
        help=(
            "Optional hard cap on IdleRange(P*). The default -1 disables the "
            "cap because IdleRange(P*) is already the optimized objective."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Timeout per run in seconds",
    )

    parser.add_argument(
        "--csv",
        default="table3_results.csv",
        help="Aggregated Table-3 CSV path",
    )

    parser.add_argument(
        "--long-csv",
        default=None,
        help=(
            "Detailed per-run CSV path. By default, a file named "
            "<csv-stem>_detailed.csv is created beside --csv."
        ),
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
    return None if value < 0 else value


def selected_solvers(choice: str) -> list[str]:
    return SOLVERS if choice == "all" else [choice]


def selected_precedence_modes(choice: str) -> list[str]:
    return PRECEDENCE_MODES if choice == "both" else [choice]


def selected_precedence_edge_modes(choice: str) -> list[str]:
    return PRECEDENCE_EDGE_MODES if choice == "both" else [choice]


def selected_objective_modes(choice: str) -> list[str]:
    return OBJECTIVE_MODES if choice == "both" else [choice]


def selected_variants(choice: str) -> list[str]:
    return VARIANTS if choice == "all" else [choice]


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
        raise FileNotFoundError(f"No .dzn files found in {folder}")

    return files


def status_to_sat_result(status: str | None) -> str:
    """Convert solver-specific statuses to the requested SAT/UNSAT result."""

    normalized = (status or "ERROR").upper()

    if normalized in {
        "OPTIMAL",
        "SAT",
        "SATISFIABLE",
        "OPTIMUM FOUND",
    }:
        return "SAT"

    if normalized in {
        "UNSAT",
        "UNSATISFIABLE",
    }:
        return "UNSAT"

    if normalized == "TIMEOUT":
        return "TIMEOUT"

    return "ERROR"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finalize_formula_metrics(payload: dict[str, Any]) -> None:
    """Normalize hard/soft/optimization clause counts and compute their total.

    Current solver results expose ``n_clauses`` as the base hard-CNF count.
    MaxSAT additionally exposes ``n_soft``. Future solver versions may expose
    ``n_optimization_clauses`` or ``n_total_clauses``; those are honored here.
    """

    n_vars = payload.get("n_vars")
    hard = payload.get("n_hard_clauses")
    if hard is None:
        hard = payload.get("n_clauses")

    soft = payload.get("n_soft_clauses")
    if soft is None:
        soft = payload.get("n_soft", 0)

    optimization = payload.get("n_optimization_clauses", 0)
    explicit_total = payload.get("n_total_clauses")

    payload["n_vars"] = n_vars
    payload["n_hard_clauses"] = hard
    payload["n_soft_clauses"] = soft
    payload["n_optimization_clauses"] = optimization

    if explicit_total is not None:
        total = explicit_total
    elif hard is None:
        total = None
    else:
        total = (
            _safe_int(hard)
            + _safe_int(soft)
            + _safe_int(optimization)
        )

    payload["n_total_clauses"] = total
    # Keep a simple user-facing alias for CSV readers.
    payload["total_clauses"] = total


def _format_number(value: Any) -> str:
    return "N/A" if value is None else str(value)


def _format_memory(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f} MB"


def _default_detailed_csv_path(csv_path: str) -> Path:
    path = Path(csv_path)
    suffix = path.suffix or ".csv"
    return path.with_name(f"{path.stem}_detailed{suffix}")


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
    return ",".join(str(v + 1) if v >= 0 else "-" for v in values)


def serialize_schedule(
    meetings_per_slot: list[list[int]] | None,
) -> str:
    if meetings_per_slot is None:
        return ""

    parts = []
    for slot, meetings in enumerate(meetings_per_slot, start=1):
        text = " ".join(f"M{m + 1}" for m in meetings)
        parts.append(f"{slot}:{text}")

    return " | ".join(parts)


# =========================================================
# SOLVER CREATION AND WORKER
# =========================================================


def _solver_module_and_classes(solver_name: str) -> tuple[Any, tuple[str, ...]]:
    if solver_name == "incremental":
        return IncrementalSAT_Solver, (
            "B2BIncrementalSATSolver",
            "IncrementalSATSolver",
        )

    if solver_name == "multiple":
        return Multiple_SAT, (
            "B2BMultipleSATSolver",
            "MultipleSATSolver",
        )

    if solver_name == "maxsat":
        return MaxSAT_Solver, (
            "B2BMaxSATSolver",
            "MaxSATSolver",
        )

    raise ValueError(f"Unknown solver: {solver_name}")


def _create_solver_object(
    solver_name: str,
    instance_path: str,
    fairness_limit: int | None,
    precedence_mode: str,
    precedence_edge_mode: str,
    encoding_variant: str,
    objective_mode: str,
) -> tuple[Any, Any | None]:
    module, class_names = _solver_module_and_classes(solver_name)

    for class_name in class_names:
        solver_class = getattr(module, class_name, None)
        if solver_class is None:
            continue

        solver_object = solver_class(
            instance_or_path=instance_path,
            fairness_limit=fairness_limit,
            precedence_mode=precedence_mode,
            precedence_edge_mode=precedence_edge_mode,
            encoding_variant=encoding_variant,
            objective_mode=objective_mode,
        )
        return module, solver_object

    return module, None


def _artifact_metadata(solver_name: str, solver_object: Any) -> dict[str, Any]:
    artifacts = getattr(solver_object, "artifacts", None)
    if artifacts is None:
        return {}

    hard_clauses = getattr(artifacts, "n_clauses", None)
    objective_lits = getattr(artifacts, "objective_lits", []) or []

    # Objective literals become actual unit soft clauses only in the MaxSAT WCNF.
    soft_clauses = len(objective_lits) if solver_name == "maxsat" else 0

    metadata = {
        "n_vars": getattr(artifacts, "n_vars", None),
        "n_hard_clauses": hard_clauses,
        "n_soft_clauses": soft_clauses,
        "n_optimization_clauses": 0,
        "objective": getattr(artifacts, "objective_name", None),
        "objective_mode": getattr(artifacts, "objective_mode", None),
        "objective_participant_count": len(
            getattr(artifacts, "objective_participants", ())
        ),
        "objective_participants": serialize_list(
            [
                p + 1
                for p in getattr(artifacts, "objective_participants", ())
            ]
        ),
        "precedence_edge_mode": getattr(
            artifacts,
            "precedence_edge_mode",
            None,
        ),
        "precedence_direct_edges": getattr(
            artifacts,
            "precedence_direct_edges",
            None,
        ),
        "precedence_source_added_edges": getattr(
            artifacts,
            "precedence_source_added_edges",
            None,
        ),
        "precedence_encoded_edges": getattr(
            artifacts,
            "precedence_encoded_edges",
            None,
        ),
        "precedence_full_closure_edges": getattr(
            artifacts,
            "precedence_transitive_edges",
            None,
        ),
        "n_primary_objective_lits": len(objective_lits),
        "n_secondary_objective_lits": len(
            getattr(artifacts, "secondary_objective_lits", []) or []
        ),
    }
    _finalize_formula_metrics(metadata)
    return metadata


def _build_worker_payload(
    result: dict[str, Any],
    solver_name: str,
    precedence_mode: str,
    precedence_edge_mode: str,
    encoding_variant: str,
    objective_mode: str,
    runtime_s: float,
) -> dict[str, Any]:
    stats = result.get("stats")

    payload: dict[str, Any] = {
        "message_type": "result",
        "status": result.get("status", "ERROR"),
        "solver": solver_name,
        "precedence_mode": precedence_mode,
        "precedence_edge_mode": precedence_edge_mode,
        "encoding_variant": encoding_variant,
        "runtime_s": round(runtime_s, 6),
        "objective": result.get("objective"),
        "objective_mode": result.get("objective_mode", objective_mode),
        "objective_value": result.get("objective_value"),
        "secondary_objective_value": result.get("secondary_objective_value"),
        "secondary_proven_optimum": result.get("secondary_proven_optimum"),
        "tertiary_objective_value": result.get("tertiary_objective_value"),
        "tertiary_proven_optimum": result.get("tertiary_proven_optimum"),
        "total_breaks": None if stats is None else stats.total_breaks,
        "fairness_gap": None if stats is None else stats.fairness_gap,
        "idle_range": None if stats is None else stats.idle_range,
        "all_participant_idle_range": (
            None if stats is None else stats.all_participant_idle_range
        ),
        "objective_participant_count": (
            None if stats is None else len(stats.objective_participants)
        ),
        "objective_participants": (
            "" if stats is None else serialize_list(stats.objective_participant_ids)
        ),
        "participant_breaks": (
            "" if stats is None else serialize_list(stats.participant_breaks)
        ),
        "busy_participants_per_slot": (
            ""
            if stats is None
            else serialize_list(stats.busy_participants_per_slot)
        ),
        "assignment": serialize_assignment(result.get("assignment")),
        "schedule_by_slot": (
            "" if stats is None else serialize_schedule(stats.meetings_per_slot)
        ),
        "validation_errors": "; ".join(
            result.get("validation_errors", [])
        ),
        "n_vars": result.get("n_vars"),
        "n_clauses": result.get("n_clauses"),
        "n_hard_clauses": result.get("n_hard_clauses"),
        "n_soft": result.get("n_soft"),
        "n_soft_clauses": result.get("n_soft_clauses"),
        "n_optimization_clauses": result.get(
            "n_optimization_clauses",
            0,
        ),
        "n_total_clauses": result.get("n_total_clauses"),
        "n_primary_objective_lits": result.get("n_primary_objective_lits"),
        "n_secondary_objective_lits": result.get("n_secondary_objective_lits"),
        "precedence_direct_edges": result.get("precedence_direct_edges"),
        "precedence_source_added_edges": result.get(
            "precedence_source_added_edges"
        ),
        "precedence_encoded_edges": result.get("precedence_encoded_edges"),
        "precedence_full_closure_edges": result.get(
            "precedence_full_closure_edges"
        ),
        "enabled_constraints": " | ".join(
            result.get("enabled_constraints", [])
        ),
        "error_type": "",
        "error_message": "",
    }

    payload["sat_result"] = status_to_sat_result(payload["status"])
    _finalize_formula_metrics(payload)
    return payload


def _worker(
    solver_name: str,
    instance_path: str,
    fairness_limit: int | None,
    precedence_mode: str,
    precedence_edge_mode: str,
    encoding_variant: str,
    objective_mode: str,
    verbose: bool,
    queue: mp.Queue,
) -> None:
    started = time.perf_counter()

    try:
        module, solver_object = _create_solver_object(
            solver_name=solver_name,
            instance_path=instance_path,
            fairness_limit=fairness_limit,
            precedence_mode=precedence_mode,
            precedence_edge_mode=precedence_edge_mode,
            encoding_variant=encoding_variant,
            objective_mode=objective_mode,
        )

        if solver_object is not None:
            metadata = _artifact_metadata(solver_name, solver_object)
            if metadata:
                queue.put({
                    "message_type": "metadata",
                    **metadata,
                })
            result = solver_object.solve(verbose=verbose)
        else:
            # Compatibility fallback for modules that only expose solve_b2b().
            result = module.solve_b2b(
                instance_or_path=instance_path,
                fairness_limit=fairness_limit,
                precedence_mode=precedence_mode,
                precedence_edge_mode=precedence_edge_mode,
                encoding_variant=encoding_variant,
                objective_mode=objective_mode,
                verbose=verbose,
            )

        queue.put(
            _build_worker_payload(
                result=result,
                solver_name=solver_name,
                precedence_mode=precedence_mode,
                precedence_edge_mode=precedence_edge_mode,
                encoding_variant=encoding_variant,
                objective_mode=objective_mode,
                runtime_s=time.perf_counter() - started,
            )
        )

    except Exception as exc:
        payload: dict[str, Any] = {
            "message_type": "result",
            "status": "ERROR",
            "sat_result": "ERROR",
            "solver": solver_name,
            "precedence_mode": precedence_mode,
            "precedence_edge_mode": precedence_edge_mode,
            "encoding_variant": encoding_variant,
            "runtime_s": round(time.perf_counter() - started, 6),
            "objective": (
                "internal_idle_slot_range_pstar"
                if objective_mode == "idle-range"
                else "lexicographic_internal_idle_range_pstar_then_idle_max"
            ),
            "objective_mode": objective_mode,
            "secondary_objective_value": None,
            "secondary_proven_optimum": None,
            "tertiary_objective_value": None,
            "tertiary_proven_optimum": None,
            "total_breaks": None,
            "fairness_gap": None,
            "idle_range": None,
            "all_participant_idle_range": None,
            "objective_participant_count": None,
            "objective_participants": "",
            "participant_breaks": "",
            "busy_participants_per_slot": "",
            "assignment": "",
            "schedule_by_slot": "",
            "validation_errors": "",
            "n_vars": None,
            "n_clauses": None,
            "n_hard_clauses": None,
            "n_soft_clauses": None,
            "n_optimization_clauses": 0,
            "n_total_clauses": None,
            "total_clauses": None,
            "enabled_constraints": "",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        queue.put(payload)


# =========================================================
# MEMORY AND TIMEOUT WRAPPER
# =========================================================


def _process_tree_rss_bytes(pid: int) -> int | None:
    """Return current RSS for the worker and all recursive child processes."""

    if psutil is None:
        return None

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return None

    total = 0
    found = False

    for process in processes:
        try:
            total += process.memory_info().rss
            found = True
        except (psutil.Error, OSError):
            continue

    return total if found else None


def _drain_worker_queue(
    queue: mp.Queue,
    metadata: dict[str, Any],
    result_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    while True:
        try:
            message = queue.get_nowait()
        except queue_module.Empty:
            break

        if message.get("message_type") == "metadata":
            metadata.update(
                {
                    key: value
                    for key, value in message.items()
                    if key != "message_type"
                }
            )
        elif message.get("message_type") == "result":
            result_payload = message

    return result_payload


def _base_terminal_payload(
    status: str,
    solver_name: str,
    precedence_mode: str,
    precedence_edge_mode: str,
    encoding_variant: str,
    objective_mode: str,
    runtime_s: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "sat_result": status_to_sat_result(status),
        "solver": solver_name,
        "precedence_mode": precedence_mode,
        "precedence_edge_mode": precedence_edge_mode,
        "encoding_variant": encoding_variant,
        "runtime_s": round(runtime_s, 6),
        "objective": None,
        "objective_mode": objective_mode,
        "secondary_objective_value": None,
        "secondary_proven_optimum": None,
        "tertiary_objective_value": None,
        "tertiary_proven_optimum": None,
        "total_breaks": None,
        "fairness_gap": None,
        "idle_range": None,
        "all_participant_idle_range": None,
        "objective_participant_count": None,
        "objective_participants": None,
        "participant_breaks": "",
        "busy_participants_per_slot": "",
        "assignment": "",
        "schedule_by_slot": "",
        "validation_errors": "",
        "n_vars": None,
        "n_clauses": None,
        "n_hard_clauses": None,
        "n_soft_clauses": None,
        "n_optimization_clauses": 0,
        "n_total_clauses": None,
        "total_clauses": None,
        "enabled_constraints": "",
        "error_type": "",
        "error_message": "",
    }
    return payload


def run_with_timeout(
    solver_name: str,
    instance_path: Path,
    fairness_limit: int | None,
    precedence_mode: str,
    precedence_edge_mode: str,
    encoding_variant: str,
    objective_mode: str,
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
            precedence_edge_mode,
            encoding_variant,
            objective_mode,
            verbose,
            queue,
        ),
    )

    started = time.perf_counter()
    deadline = started + timeout_s
    peak_rss_bytes: int | None = None
    metadata: dict[str, Any] = {}
    result_payload: dict[str, Any] | None = None

    proc.start()

    timed_out = False
    while proc.is_alive():
        current_rss = _process_tree_rss_bytes(proc.pid)
        if current_rss is not None:
            peak_rss_bytes = max(peak_rss_bytes or 0, current_rss)

        result_payload = _drain_worker_queue(
            queue,
            metadata,
            result_payload,
        )

        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            timed_out = True
            break

        proc.join(min(MEMORY_SAMPLE_INTERVAL_S, remaining))

    # One last sample before termination/reaping when possible.
    current_rss = _process_tree_rss_bytes(proc.pid)
    if current_rss is not None:
        peak_rss_bytes = max(peak_rss_bytes or 0, current_rss)

    if timed_out and proc.is_alive():
        proc.terminate()
        proc.join()

        result_payload = _base_terminal_payload(
            status="TIMEOUT",
            solver_name=solver_name,
            precedence_mode=precedence_mode,
            precedence_edge_mode=precedence_edge_mode,
            encoding_variant=encoding_variant,
            objective_mode=objective_mode,
            runtime_s=time.perf_counter() - started,
        )
    else:
        proc.join()

        # Multiprocessing.Queue may flush shortly after the child exits.
        grace_deadline = time.perf_counter() + QUEUE_RESULT_GRACE_S
        while result_payload is None and time.perf_counter() < grace_deadline:
            result_payload = _drain_worker_queue(
                queue,
                metadata,
                result_payload,
            )
            if result_payload is None:
                time.sleep(0.01)

        if result_payload is None:
            result_payload = _base_terminal_payload(
                status="ERROR",
                solver_name=solver_name,
                precedence_mode=precedence_mode,
                precedence_edge_mode=precedence_edge_mode,
                encoding_variant=encoding_variant,
                objective_mode=objective_mode,
                runtime_s=time.perf_counter() - started,
            )
            result_payload["error_type"] = "NoWorkerPayload"
            result_payload["error_message"] = "Worker returned nothing"

    # Preserve early model metadata even when the solve is terminated by timeout.
    for key, value in metadata.items():
        if result_payload.get(key) is None:
            result_payload[key] = value

    result_payload["sat_result"] = status_to_sat_result(
        result_payload.get("status")
    )
    result_payload["peak_memory_mb"] = (
        None
        if peak_rss_bytes is None
        else round(peak_rss_bytes / (1024 * 1024), 3)
    )
    result_payload["memory_metric"] = "peak_process_tree_rss_mb"
    _finalize_formula_metrics(result_payload)

    queue.close()
    queue.join_thread()
    return result_payload


# =========================================================
# OUTPUT FORMATTERS
# =========================================================


def format_table3_cell(result: dict[str, Any]) -> str:
    sat_result = result.get("sat_result") or status_to_sat_result(
        result.get("status")
    )
    runtime = result.get("runtime_s")
    objective_value = result.get("idle_range")
    if objective_value is None:
        objective_value = result.get("fairness_gap")

    if sat_result == "TIMEOUT":
        return f"TO {objective_value if objective_value is not None else '-'}"

    if sat_result == "UNSAT":
        return "UNSAT"

    if sat_result == "SAT":
        if runtime is None:
            return f"SAT ? {objective_value}"
        return f"{runtime:.1f} {objective_value}"

    return "ERR"


def print_detailed_results(results: list[dict[str, Any]]) -> None:
    print("\n")
    width = 190
    print("=" * width)
    print("DETAILED PER-RUN RESULTS")
    print("=" * width)

    header = (
        f"{'instance':<28}"
        f"{'solver':<13}"
        f"{'precedence':<13}"
        f"{'edges':<17}"
        f"{'objective':<16}"
        f"{'variant':<10}"
        f"{'result':<10}"
        f"{'time(s)':>12}"
        f"{'variables':>14}"
        f"{'clauses(total)':>18}"
        f"{'peak memory(MB)':>18}"
    )
    print(header)
    print("-" * width)

    for row in results:
        runtime = row.get("runtime_s")
        runtime_text = "N/A" if runtime is None else f"{runtime:.6f}"
        memory = row.get("peak_memory_mb")
        memory_text = "N/A" if memory is None else f"{memory:.3f}"

        print(
            f"{row['instance']:<28}"
            f"{row['solver']:<13}"
            f"{row['precedence_mode']:<13}"
            f"{row['precedence_edge_mode']:<17}"
            f"{row['objective_mode']:<16}"
            f"{row['encoding_variant']:<10}"
            f"{row.get('sat_result', 'ERROR'):<10}"
            f"{runtime_text:>12}"
            f"{_format_number(row.get('n_vars')):>14}"
            f"{_format_number(row.get('n_total_clauses')):>18}"
            f"{memory_text:>18}"
        )

    print("=" * width)


def write_detailed_csv(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    preferred_fields = [
        "instance",
        "sat_result",
        "status",
        "runtime_s",
        "n_vars",
        "n_hard_clauses",
        "n_soft_clauses",
        "n_optimization_clauses",
        "n_total_clauses",
        "peak_memory_mb",
        "memory_metric",
        "solver",
        "precedence_mode",
        "precedence_edge_mode",
        "staircase",
        "encoding_variant",
        "objective",
        "objective_mode",
        "secondary_objective_value",
        "secondary_proven_optimum",
        "tertiary_objective_value",
        "tertiary_proven_optimum",
        "objective_participant_count",
        "objective_participants",
        "total_breaks",
        "fairness_gap",
        "idle_range",
        "all_participant_idle_range",
        "precedence_direct_edges",
        "precedence_source_added_edges",
        "precedence_encoded_edges",
        "precedence_full_closure_edges",
        "n_primary_objective_lits",
        "n_secondary_objective_lits",
        "participant_breaks",
        "busy_participants_per_slot",
        "assignment",
        "schedule_by_slot",
        "validation_errors",
        "enabled_constraints",
        "error_type",
        "error_message",
    ]

    extra_fields = sorted(
        {
            key
            for row in results
            for key in row
            if key not in preferred_fields
        }
    )
    fieldnames = preferred_fields + extra_fields

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(results)


# =========================================================
# MAIN
# =========================================================


def main() -> None:
    args = parse_args()

    fairness_limit = normalize_fairness(args.fairness)
    instances = collect_instances(args.instance, args.data_dir)
    solver_list = selected_solvers(args.solver)
    precedence_modes = selected_precedence_modes(args.precedence_mode)
    precedence_edge_modes = selected_precedence_edge_modes(
        args.precedence_edges
    )
    objective_modes = selected_objective_modes(args.objective_mode)
    variants = selected_variants(args.encoding_variant)

    results: list[dict[str, Any]] = []

    total_runs = (
        len(instances)
        * len(solver_list)
        * len(precedence_modes)
        * len(precedence_edge_modes)
        * len(objective_modes)
        * len(variants)
    )
    current_run = 0

    print("\n" + "=" * 120)
    print("B2B SAT BENCHMARK")
    print("=" * 120)

    if psutil is None:
        print(
            "WARNING: psutil is not installed. peak_memory_mb will be N/A. "
            "Install it with: pip install psutil"
        )

    for instance_path in instances:
        instance_name = instance_path.stem

        for precedence_mode in precedence_modes:
            staircase = "yes" if precedence_mode == "staircase" else "no"

            for precedence_edge_mode in precedence_edge_modes:
                for objective_mode in objective_modes:
                    for solver_name in solver_list:
                        for variant in variants:
                            current_run += 1

                            print(
                                f"\n[{current_run}/{total_runs}] "
                                f"{instance_name} | "
                                f"{solver_name} | "
                                f"{precedence_mode} | "
                                f"{precedence_edge_mode} | "
                                f"{objective_mode} | "
                                f"{variant}"
                            )

                            result = run_with_timeout(
                                solver_name=solver_name,
                                instance_path=instance_path,
                                fairness_limit=fairness_limit,
                                precedence_mode=precedence_mode,
                                precedence_edge_mode=precedence_edge_mode,
                                encoding_variant=variant,
                                objective_mode=objective_mode,
                                timeout_s=args.timeout,
                                verbose=args.verbose,
                            )

                            row = {
                                "instance": instance_name,
                                "staircase": staircase,
                                **result,
                            }
                            results.append(row)

                            print(
                                f"   result={result.get('sat_result')} | "
                                f"status={result.get('status')} | "
                                f"time={result.get('runtime_s')}s | "
                                f"variables={_format_number(result.get('n_vars'))} | "
                                f"clauses={_format_number(result.get('n_total_clauses'))} | "
                                f"memory={_format_memory(result.get('peak_memory_mb'))} | "
                                f"IdleRange(P*)={result.get('idle_range')} | "
                                # The optimized tie-breaker. IdleSum is only an
                                # incidental property of the returned schedule
                                # unless the solver actually minimizes it, so it
                                # may differ between equally optimal runs.
                                f"Secondary={result.get('secondary_objective_value')} | "
                                f"IdleSum={result.get('total_breaks')}"
                            )

                            if result.get("status") == "ERROR":
                                print(
                                    f"   error={result.get('error_type')}: "
                                    f"{result.get('error_message')}"
                                )

    # =====================================================
    # DETAILED OUTPUT
    # =====================================================

    print_detailed_results(results)

    detailed_csv_path = (
        Path(args.long_csv)
        if args.long_csv
        else _default_detailed_csv_path(args.csv)
    )
    write_detailed_csv(detailed_csv_path, results)
    print(f"\nDetailed CSV exported to: {detailed_csv_path}")

    # =====================================================
    # AGGREGATE TABLE 3
    # =====================================================

    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    for result in results:
        key = (
            result["instance"],
            result["staircase"],
            result["solver"],
            result["precedence_edge_mode"],
            result["objective_mode"],
        )

        if key not in grouped:
            grouped[key] = {
                "instance": result["instance"],
                "staircase": result["staircase"],
                "solver": result["solver"],
                "precedence_edges": result["precedence_edge_mode"],
                "objective": result["objective_mode"],
            }

        grouped[key][result["encoding_variant"]] = format_table3_cell(result)

    table3_rows = []
    for row in grouped.values():
        for variant in VARIANTS:
            row.setdefault(variant, "-")
        table3_rows.append(row)

    table3_rows.sort(
        key=lambda item: (
            item["instance"],
            item["solver"],
            item["staircase"],
            item["precedence_edges"],
            item["objective"],
        )
    )

    print("\n")
    print("=" * 150)

    header = (
        f"{'instance':<30}"
        f"{'stairs':<10}"
        f"{'solver':<15}"
        f"{'edges':<18}"
        f"{'objective':<16}"
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
            f"{row['precedence_edges']:<18}"
            f"{row['objective']:<16}"
        )
        for variant in VARIANTS:
            line += f"{row[variant]:<18}"
        print(line)

    print("=" * 150)

    # =====================================================
    # EXPORT TABLE 3 CSV
    # =====================================================

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_fields = [
        "instance",
        "staircase",
        "solver",
        "precedence_edges",
        "objective",
        *VARIANTS,
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(table3_rows)

    print(f"\nTable-3 CSV exported to: {csv_path}")

    # =====================================================
    # SUMMARY
    # =====================================================

    print("\nSUMMARY")

    for solver_name in solver_list:
        sat_count = sum(
            1
            for result in results
            if result["solver"] == solver_name
            and result["sat_result"] == "SAT"
        )
        unsat_count = sum(
            1
            for result in results
            if result["solver"] == solver_name
            and result["sat_result"] == "UNSAT"
        )
        timeout_count = sum(
            1
            for result in results
            if result["solver"] == solver_name
            and result["sat_result"] == "TIMEOUT"
        )
        error_count = sum(
            1
            for result in results
            if result["solver"] == solver_name
            and result["sat_result"] == "ERROR"
        )

        print(
            f"{solver_name:<15}"
            f" sat={sat_count:<5}"
            f" unsat={unsat_count:<5}"
            f" timeout={timeout_count:<5}"
            f" error={error_count:<5}"
        )

    print("\nDone.")


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    mp.freeze_support()
    main()
