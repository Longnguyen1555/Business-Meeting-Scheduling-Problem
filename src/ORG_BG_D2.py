from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pysat.card import CardEnc, EncType
from pysat.examples.rc2 import RC2
from pysat.formula import CNF, IDPool, WCNF

from B2B_Instance import B2BInstance, read_instance, validate_schedule_assignment
from Excel_Results import FORMULA_SCOPE, RUNTIME_SCOPE, safe_workbook_name, write_instance_workbook
from Journal_Metrics import evaluate_journal_schedule
from Main import collect_instances, experiment_metadata, instance_result_metadata, write_detailed_csv
from MaxSAT_Solver import (
    UWRMAXSAT_NOT_FOUND_MESSAGE,
    executable_sha256,
    resolve_uwrmaxsat_binary,
)

try:
    import psutil
except ImportError:  # pragma: no cover - production requirements include psutil.
    psutil = None


ORG_BG_CONFIGURATION_LABEL = "ORG-F-PW-DE-PHC-BGD2-UW-OBIC12P"
ORG_BG_CONFIGURATION_ID = (
    "baseline2__model-org_historical_maxsat__m-full__p-pairwise__"
    "g-direct__b-per_slot_hole_cardinality__o-break_groups_d2__"
    "s-uwrmaxsat__i-old_best_ic12plus__fairness-d2"
)
ORG_BG_ENCODING_VARIANT = "org_historical_bg_d2_old_best"
ORG_BG_IMPLIED_PACKAGE_CODE = "OBIC12P"
ORG_BG_IMPLIED_PACKAGE_NAME = "OldBestIC12+"
MEMORY_SAMPLE_INTERVAL_SECONDS = 0.05


def _serialize(values: Any) -> str:
    if values is None:
        return ""
    return ",".join(str(value) for value in values)


def _parse_uwr_output(output: str) -> tuple[str | None, int | None, list[int]]:
    status: str | None = None
    cost: int | None = None
    model: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("s "):
            status = line[2:].strip()
        elif line.startswith("o "):
            try:
                cost = int(line[2:].strip())
            except ValueError:
                pass
        elif line.startswith("v "):
            for token in line[2:].split():
                try:
                    literal = int(token)
                except ValueError:
                    continue
                if literal:
                    model.append(literal)
    return status, cost, model


class _MemorySampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.peak_bytes: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        if psutil is None:
            return
        try:
            root = psutil.Process(self.pid)
            processes = [root, *root.children(recursive=True)]
        except (psutil.Error, OSError):
            return
        total = 0
        found = False
        for process in processes:
            try:
                total += process.memory_info().rss
                found = True
            except (psutil.Error, OSError):
                pass
        if found:
            self.peak_bytes = max(self.peak_bytes or 0, total)

    def _run(self) -> None:
        while not self._stop.wait(MEMORY_SAMPLE_INTERVAL_SECONDS):
            self._sample()

    def start(self) -> "_MemorySampler":
        self._sample()
        if psutil is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def stop_mb(self) -> float | None:
        self._sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()
        return (
            None
            if self.peak_bytes is None
            else round(self.peak_bytes / (1024 * 1024), 3)
        )


class HistoricalBGBaseline:
    """Full-domain historical break-group model with the hard d=2 cap.

    This is intentionally independent from ``B2BSATModel``. It keeps the
    historical per-slot prefix/hole/cardinality construction so that the
    ORG/full versus Compact/full comparison does not share objective helpers.
    All decoded metrics are recomputed by ``Journal_Metrics``.
    """

    def __init__(self, instance: B2BInstance) -> None:
        self.inst = instance
        self.vpool = IDPool()
        self.cnf = CNF()
        self.x_vars: dict[tuple[int, int], int] = {}
        self.y_vars: dict[tuple[int, int], int] = {}
        self.prefix_vars: dict[tuple[int, int], int] = {}
        self.hole_end_vars: dict[tuple[int, int], int] = {}
        self.thresholds: list[list[int]] = [
            [] for _ in range(instance.n_business)
        ]
        self.range_lits: list[int] = []
        self._built = False

    def x(self, meeting: int, slot: int) -> int:
        return self.vpool.id(("x", meeting, slot))

    def y(self, participant: int, slot: int) -> int:
        return self.vpool.id(("y", participant, slot))

    def prefix(self, participant: int, slot: int) -> int:
        return self.vpool.id(("prefix", participant, slot))

    def hole_end(self, participant: int, slot: int) -> int:
        return self.vpool.id(("hole_end", participant, slot))

    def threshold(self, participant: int, amount: int) -> int:
        return self.vpool.id(("groups_at_least", participant, amount))

    def _append_cardinality(self, encoding: Any) -> None:
        self.cnf.extend(encoding.clauses)

    def _at_most(self, literals: list[int], bound: int) -> None:
        if bound < 0:
            self.cnf.append([])
        elif bound == 0:
            self.cnf.extend([[-literal] for literal in literals])
        elif bound < len(literals):
            self._append_cardinality(
                CardEnc.atmost(
                    lits=literals,
                    bound=bound,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
            )

    def _exactly_one(self, literals: list[int]) -> None:
        if not literals:
            self.cnf.append([])
            return
        self._append_cardinality(
            CardEnc.equals(
                lits=literals,
                bound=1,
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
        )

    def _build_hard_schedule(self) -> None:
        inst = self.inst
        for meeting in range(inst.n_meetings):
            for slot in range(inst.n_total_slots):
                self.x_vars[meeting, slot] = self.x(meeting, slot)
        for participant in range(inst.n_business):
            for slot in range(inst.n_total_slots):
                self.y_vars[participant, slot] = self.y(participant, slot)
                self.prefix_vars[participant, slot] = self.prefix(
                    participant,
                    slot,
                )

        for participant, meetings in enumerate(inst.meetings_by_business):
            for slot in range(inst.n_total_slots):
                self._at_most(
                    [self.x(meeting, slot) for meeting in meetings],
                    1,
                )

        for meeting, (_, _, session) in enumerate(inst.requested):
            if session == 1:
                allowed = set(range(inst.n_morning_slots))
            elif session == 2:
                allowed = set(range(inst.n_morning_slots, inst.n_total_slots))
            else:
                allowed = set(range(inst.n_total_slots))
            self._exactly_one(
                [self.x(meeting, slot) for slot in sorted(allowed)]
            )
            for slot in range(inst.n_total_slots):
                if slot not in allowed:
                    self.cnf.append([-self.x(meeting, slot)])

        for slot in range(inst.n_total_slots):
            self._at_most(
                [self.x(meeting, slot) for meeting in range(inst.n_meetings)],
                inst.n_tables,
            )

        for meeting, fixed_slot in enumerate(inst.fixed):
            if fixed_slot is not None:
                self.cnf.append([self.x(meeting, fixed_slot)])
        for participant, forbidden_slots in enumerate(inst.forbidden):
            for slot in forbidden_slots:
                for meeting in inst.meetings_by_business[participant]:
                    self.cnf.append([-self.x(meeting, slot)])

        for successor, predecessors in enumerate(inst.precedences):
            for predecessor in predecessors:
                for successor_slot in range(inst.n_total_slots):
                    for predecessor_slot in range(
                        successor_slot,
                        inst.n_total_slots,
                    ):
                        self.cnf.append(
                            [
                                -self.x(predecessor, predecessor_slot),
                                -self.x(successor, successor_slot),
                            ]
                        )

        for participant, meetings in enumerate(inst.meetings_by_business):
            for slot in range(inst.n_total_slots):
                scheduled = [self.x(meeting, slot) for meeting in meetings]
                for literal in scheduled:
                    self.cnf.append([-literal, self.y(participant, slot)])
                self.cnf.append([-self.y(participant, slot), *scheduled])

        # Historical implied constraints (43)--(44).
        for participant in range(inst.n_business):
            self._append_cardinality(
                CardEnc.equals(
                    lits=[
                        self.y(participant, slot)
                        for slot in range(inst.n_total_slots)
                    ],
                    bound=inst.n_meetings_business[participant],
                    vpool=self.vpool,
                    encoding=EncType.cardnetwrk,
                )
            )
        for slot in range(inst.n_total_slots):
            self._at_most(
                [self.y(participant, slot) for participant in range(inst.n_business)],
                2 * inst.n_tables,
            )

    def _build_historical_objective(self) -> None:
        inst = self.inst
        for participant in range(inst.n_business):
            for slot in range(inst.n_total_slots):
                prefix = self.prefix(participant, slot)
                used = self.y(participant, slot)
                if slot == 0:
                    self.cnf.append([-prefix, used])
                    self.cnf.append([prefix, -used])
                else:
                    previous = self.prefix(participant, slot - 1)
                    self.cnf.append([-used, prefix])
                    self.cnf.append([-previous, prefix])
                    self.cnf.append([used, previous, -prefix])

            ends: list[int] = []
            for slot in range(inst.n_total_slots - 1):
                end = self.hole_end(participant, slot)
                self.hole_end_vars[participant, slot] = end
                ends.append(end)
                current = self.y(participant, slot)
                following = self.y(participant, slot + 1)
                prefix = self.prefix(participant, slot)
                # Exact version of historical endHole semantics.
                self.cnf.append([-end, following])
                self.cnf.append([-end, -current])
                self.cnf.append([-end, prefix])
                self.cnf.append([-following, current, -prefix, end])

            upper = min(inst.max_breaks_per_participant, len(ends))
            participant_thresholds: list[int] = []
            for amount in range(1, upper + 1):
                threshold = self.threshold(participant, amount)
                participant_thresholds.append(threshold)
                at_least = CardEnc.atleast(
                    lits=ends,
                    bound=amount,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
                for clause in at_least.clauses:
                    self.cnf.append([-threshold, *clause])
                at_most = CardEnc.atmost(
                    lits=ends,
                    bound=amount - 1,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
                for clause in at_most.clauses:
                    self.cnf.append([threshold, *clause])
            for index in range(1, len(participant_thresholds)):
                self.cnf.append(
                    [
                        -participant_thresholds[index],
                        participant_thresholds[index - 1],
                    ]
                )
            self.thresholds[participant] = participant_thresholds

        global_upper = max((len(values) for values in self.thresholds), default=0)
        for amount in range(1, global_upper + 1):
            maximum = self.vpool.id(("group_range_max", amount))
            minimum = self.vpool.id(("group_range_min", amount))
            difference = self.vpool.id(("group_range_difference", amount))
            present = [
                values[amount - 1]
                for values in self.thresholds
                if amount <= len(values)
            ]
            for literal in present:
                self.cnf.append([-literal, maximum])
            self.cnf.append([-maximum, *present] if present else [-maximum])
            if len(present) == inst.n_business:
                for literal in present:
                    self.cnf.append([-minimum, literal])
                self.cnf.append([-literal for literal in present] + [minimum])
            else:
                self.cnf.append([-minimum])
            self.cnf.append([-difference, maximum])
            self.cnf.append([-difference, -minimum])
            self.cnf.append([-maximum, minimum, difference])
            self.range_lits.append(difference)
        self._at_most(self.range_lits, 2)

    def build(self) -> None:
        if self._built:
            return
        self._build_hard_schedule()
        self._build_historical_objective()
        self._built = True

    def build_wcnf(self) -> WCNF:
        self.build()
        formula = WCNF()
        for clause in self.cnf.clauses:
            formula.append(clause)
        for thresholds in self.thresholds:
            for literal in thresholds:
                formula.append([-literal], weight=1)
        return formula

    def decode(self, model: list[int]) -> list[int]:
        positives = {literal for literal in model if literal > 0}
        return [
            next(
                (
                    slot
                    for slot in range(self.inst.n_total_slots)
                    if self.x(meeting, slot) in positives
                ),
                -1,
            )
            for meeting in range(self.inst.n_meetings)
        ]

    def validate_model(
        self,
        model: list[int],
        solver_cost: int,
    ) -> tuple[list[int], Any, list[str]]:
        assignment = self.decode(model)
        errors = validate_schedule_assignment(self.inst, assignment)
        metrics = evaluate_journal_schedule(
            self.inst,
            assignment,
            objective_mode="bg_d2",
        )
        positives = {literal for literal in model if literal > 0}
        encoded_groups = sum(
            literal in positives
            for thresholds in self.thresholds
            for literal in thresholds
        )
        encoded_range = sum(literal in positives for literal in self.range_lits)
        if encoded_groups != metrics.total_break_groups:
            errors.append(
                "historical threshold mismatch: "
                f"encoded={encoded_groups}, decoded={metrics.total_break_groups}"
            )
        if encoded_range != metrics.break_group_range:
            errors.append(
                "historical range mismatch: "
                f"encoded={encoded_range}, decoded={metrics.break_group_range}"
            )
        if solver_cost != metrics.total_break_groups:
            errors.append(
                f"solver-cost mismatch: {solver_cost}!={metrics.total_break_groups}"
            )
        if not metrics.historical_fairness_cap_satisfied:
            errors.append(
                f"historical fairness cap violated: {metrics.break_group_range}>2"
            )
        return assignment, metrics, errors


def _solve_formula(
    baseline: HistoricalBGBaseline,
    *,
    backend: str,
    timeout: float,
    uwrmaxsat_binary: Path | None,
) -> tuple[str, int | None, list[int] | None, str, str]:
    formula = baseline.build_wcnf()
    if backend == "rc2":
        with RC2(formula) as solver:
            model = solver.compute()
            if model is None:
                return "UNSAT", None, None, "RC2", ""
            return "OPTIMAL", int(solver.cost), model, "RC2", ""

    if uwrmaxsat_binary is None:
        raise FileNotFoundError(UWRMAXSAT_NOT_FOUND_MESSAGE)
    with tempfile.NamedTemporaryFile(prefix="org_bg_", suffix=".wcnf") as stream:
        formula.to_file(stream.name)
        command = [str(uwrmaxsat_binary), "-m", stream.name]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                start_new_session=(os.name != "nt"),
            )
            output = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            )
            raw_status, cost, model = _parse_uwr_output(output)
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(
                part.decode(errors="replace") if isinstance(part, bytes) else part
                for part in (exc.stdout, exc.stderr)
                if part
            )
            _, cost, model = _parse_uwr_output(output)
            return "TIMEOUT", cost, model or None, "UWrMaxSAT", shlex.join(command)
    normalized = (raw_status or "").upper()
    if normalized in {"UNSAT", "UNSATISFIABLE"}:
        return "UNSAT", None, None, "UWrMaxSAT", shlex.join(command)
    if normalized in {"OPTIMUM FOUND", "OPTIMAL", "OPTIMUM"} and model:
        return "OPTIMAL", cost, model, "UWrMaxSAT", shlex.join(command)
    return "ERROR", cost, model or None, "UWrMaxSAT", shlex.join(command)


def solve_instance(
    instance: B2BInstance,
    *,
    backend: str,
    timeout: float,
    uwrmaxsat_binary: Path | None,
    uwrmaxsat_sha256: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    baseline = HistoricalBGBaseline(instance)
    baseline.build()
    model_ready = time.perf_counter()
    sampler = _MemorySampler(os.getpid()).start()
    try:
        status, cost, model, solver_backend, solver_command = _solve_formula(
            baseline,
            backend=backend,
            timeout=max(0.001, timeout - (model_ready - started)),
            uwrmaxsat_binary=uwrmaxsat_binary,
        )
    finally:
        peak_memory_mb = sampler.stop_mb()
    assignment = None
    metrics = None
    errors: list[str] = []
    if model is not None and cost is not None:
        assignment, metrics, errors = baseline.validate_model(model, cost)
        if status == "OPTIMAL" and errors:
            status = "ERROR"
    finished = time.perf_counter()
    clause_lengths = [len(clause) for clause in baseline.cnf.clauses]
    soft_count = sum(len(values) for values in baseline.thresholds)
    objective_vector = (
        (metrics.total_break_groups,) if metrics is not None else None
    )
    return {
        "configuration_label": ORG_BG_CONFIGURATION_LABEL,
        "configuration_id": ORG_BG_CONFIGURATION_ID,
        "configuration_key": ORG_BG_CONFIGURATION_ID,
        "factor_m": "ORGFull",
        "factor_f": "N/A",
        "factor_p": "Pairwise",
        "factor_g": "Direct-E",
        "factor_b": "PerSlotHoleCardinality",
        "factor_o": "BreakGroupsD2",
        "factor_s": solver_backend,
        "factor_i": ORG_BG_IMPLIED_PACKAGE_NAME,
        "formalism": "MaxSAT",
        "model_family": "ORGHistorical",
        "formulation_name": "ORG-Historical-BG-d2",
        # Stable identifiers above are retained for compatibility with existing
        # manifests.  Manuscript-facing analysis must use these display fields:
        # this model is an independent reimplementation, not original source.
        "model_family_display_name": "Published-style",
        "model_configuration_display_name": "Published-style/Full",
        "implementation_provenance": "independent_reimplementation",
        "optimization_procedure_display_name": "one-shot weighted MaxSAT",
        "domain_mode": "legacy_full",
        "domain_filter_graph": "n/a",
        "precedence_encoding": "pairwise",
        "precedence_graph": "direct",
        "precedence_mode": "traditional",
        "precedence_configuration": "pairwise+direct",
        "optimization_engine": solver_backend,
        "solver": solver_backend,
        "solver_backend": solver_backend,
        "solver_version": (
            f"binary-sha256:{uwrmaxsat_sha256}"
            if backend == "uwrmaxsat"
            else "python-sat-rc2-development"
        ),
        "solver_binary": str(uwrmaxsat_binary or ""),
        "solver_binary_sha256": uwrmaxsat_sha256,
        "solver_command": solver_command,
        "encoding_variant": ORG_BG_ENCODING_VARIANT,
        "idle_encoding": "per_slot_hole_cardinality",
        "objective": "total_break_groups_subject_to_range_at_most_2",
        "objective_mode": "bg_d2",
        "objective_code": "BGD2",
        "objective_vector": _serialize(objective_vector),
        "proven_objective_vector": (
            _serialize(objective_vector) if status == "OPTIMAL" else ""
        ),
        "primary_objective_value": (
            metrics.total_break_groups if metrics is not None else cost
        ),
        "objective_value": (
            metrics.total_break_groups if metrics is not None else cost
        ),
        "best_value": cost,
        "proven_optimum": cost if status == "OPTIMAL" else None,
        "lexicographic_scalar_cost": cost,
        "objective_tier_weights": "1",
        "implied_constraints_code": ORG_BG_IMPLIED_PACKAGE_CODE,
        "status": status,
        "sat_result": (
            "SAT" if status == "OPTIMAL" else status
        ),
        "runtime_seconds": round(finished - started, 6),
        "runtime_censored": status == "TIMEOUT",
        "input_parsing_seconds": 0.0,
        "model_construction_seconds": round(model_ready - started, 6),
        "model_build_seconds": round(model_ready - started, 6),
        "solve_and_validate_seconds": round(finished - model_ready, 6),
        "runtime_scope": RUNTIME_SCOPE,
        "peak_memory_mb": peak_memory_mb,
        "memory_metric": "peak_process_tree_rss_mb",
        "formula_scope": FORMULA_SCOPE,
        "n_vars": max(baseline.vpool.top, baseline.cnf.nv),
        "n_primary_variables": instance.n_meetings * instance.n_total_slots,
        "n_auxiliary_variables": (
            max(baseline.vpool.top, baseline.cnf.nv)
            - instance.n_meetings * instance.n_total_slots
        ),
        "n_hard_clauses": len(baseline.cnf.clauses),
        "n_soft_clauses": soft_count,
        "n_total_clauses": len(baseline.cnf.clauses) + soft_count,
        "n_hard_literals": sum(clause_lengths),
        "n_soft_literals": soft_count,
        "n_total_literals": sum(clause_lengths) + soft_count,
        "max_hard_clause_length": max(clause_lengths, default=0),
        "max_soft_clause_length": 1 if soft_count else 0,
        "n_unit_hard_clauses": sum(length == 1 for length in clause_lengths),
        "n_binary_hard_clauses": sum(length == 2 for length in clause_lengths),
        "n_ternary_hard_clauses": sum(length == 3 for length in clause_lengths),
        "n_long_hard_clauses": sum(length >= 4 for length in clause_lengths),
        "soft_clause_weight": 1 if soft_count else 0,
        "soft_weight_sum": soft_count,
        "n_objective_lits": soft_count,
        "n_optimizer_calls": 1,
        "n_bound_encodings": 0,
        "full_schedule_candidates": instance.n_meetings * instance.n_total_slots,
        "active_schedule_candidates": instance.n_meetings * instance.n_total_slots,
        "total_break_groups": (
            metrics.total_break_groups if metrics is not None else None
        ),
        "break_group_range": (
            metrics.break_group_range if metrics is not None else None
        ),
        "participant_break_groups": (
            _serialize(metrics.participant_break_groups)
            if metrics is not None
            else ""
        ),
        "idle_range_pstar": (
            metrics.idle_range_pstar if metrics is not None else None
        ),
        "total_internal_idle_slots": (
            metrics.total_internal_idle_slots if metrics is not None else None
        ),
        "assignment": _serialize(assignment),
        "validation_errors": "; ".join(errors),
        "solver_message": "",
        "error_type": "ValidationError" if errors else "",
        "error_message": "; ".join(errors),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the independent historical ORG/full BG-d2 baseline."
    )
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--instance")
    inputs.add_argument("--data-dir")
    inputs.add_argument("--manifest")
    parser.add_argument(
        "--family",
        choices=["all", "original", "forbidden", "fixed", "precedence"],
        default="all",
    )
    parser.add_argument("--backend", choices=["uwrmaxsat", "rc2"], default="uwrmaxsat")
    parser.add_argument("--uwrmaxsat-bin")
    parser.add_argument("--uwrmaxsat-sha256")
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--excel-dir")
    parser.add_argument("--no-excel", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    instances = collect_instances(
        args.instance,
        args.data_dir,
        args.manifest,
        args.family,
    )
    binary = (
        resolve_uwrmaxsat_binary(args.uwrmaxsat_bin)
        if args.backend == "uwrmaxsat"
        else None
    )
    if args.backend == "uwrmaxsat" and binary is None:
        print(f"ERROR: {UWRMAXSAT_NOT_FOUND_MESSAGE}")
        return 2
    binary_sha256 = executable_sha256(binary) if binary is not None else ""
    expected_sha256 = (args.uwrmaxsat_sha256 or "").strip().lower()
    if expected_sha256 and expected_sha256 != binary_sha256:
        print(
            "ERROR: UWrMaxSAT executable SHA-256 mismatch: "
            f"expected {expected_sha256}, got {binary_sha256}"
        )
        return 2

    experiment = experiment_metadata(args, argv, runner_path=__file__)
    output_path = Path(args.csv)
    excel_dir = Path(args.excel_dir or output_path.parent / "excel_org_bg_d2")
    results: list[dict[str, Any]] = []
    for index, spec in enumerate(instances, start=1):
        started = time.perf_counter()
        instance = read_instance(spec.path)
        parsing_seconds = time.perf_counter() - started
        result = solve_instance(
            instance,
            backend=args.backend,
            timeout=max(0.001, args.timeout - parsing_seconds),
            uwrmaxsat_binary=binary,
            uwrmaxsat_sha256=binary_sha256,
        )
        result["input_parsing_seconds"] = round(parsing_seconds, 6)
        result["runtime_seconds"] = round(
            float(result["runtime_seconds"]) + parsing_seconds,
            6,
        )
        row = {**instance_result_metadata(spec), **experiment, **result}
        results.append(row)
        if not args.no_excel:
            write_instance_workbook(
                excel_dir / safe_workbook_name(spec.instance_name),
                spec.instance_name,
                [row],
            )
        print(
            f"[{index}/{len(instances)}] {spec.instance_name}: "
            f"{row['status']} vector={row['objective_vector']} "
            f"time={row['runtime_seconds']}s",
            flush=True,
        )
    write_detailed_csv(output_path, results)
    return 2 if any(row["status"] == "ERROR" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
