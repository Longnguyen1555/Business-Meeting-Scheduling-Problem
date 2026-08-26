from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from pysat.card import CardEnc, EncType
from pysat.examples.rc2 import RC2
from pysat.formula import CNF, IDPool, WCNF

from B2B_Instance import B2BInstance, read_instance, validate_schedule_assignment
from Excel_Results import (
    FORMULA_SCOPE,
    RUNTIME_SCOPE,
    safe_workbook_name,
    write_instance_workbook,
)
from Journal_Metrics import evaluate_journal_schedule
from Main import (
    collect_instances,
    experiment_metadata,
    instance_result_metadata,
    write_detailed_csv,
)
from MaxSAT_Solver import (
    UWRMAXSAT_NOT_FOUND_MESSAGE,
    executable_sha256,
    resolve_uwrmaxsat_binary,
)
from ORG_BG_D2 import _MemorySampler, _parse_uwr_output, _serialize


PAPER_CONFIGURATION_LABEL = "ORG-PAPER-F-PW-DE-CN-BGD2-UW-IC12"
PAPER_CONFIGURATION_ID = (
    "baseline_paper__model-published_maxsat__m-full__p-pairwise__"
    "g-direct__b-cardinality_network__o-break_groups_d2__"
    "s-uwrmaxsat__i-ic12__fairness-d2"
)
PAPER_ENCODING_VARIANT = "org_paper_cardinality_network_ic12"
PAPER_IMPLIED_PACKAGE_CODE = "IC12"
PAPER_IMPLIED_PACKAGE_NAME = "IC12"
PAPER_EXACTLY_ONE_ENCODING = "commander_group3"
PAPER_GENERAL_CARDINALITY_ENCODING = "cardinality_network"
PAPER_SORTING_NETWORK_ENCODING = "batcher_odd_even_exact"
PAPER_MAX_MIN_ENCODING = "published_one_way_approximation"
PAPER_DIFFERENCE_ENCODING = "exact_xor"
PAPER_FAIRNESS_D = 2


class PaperExactBGBaseline:
    """Published MaxSAT B2BSOP-d encoding, independently reimplemented.

    The formulation follows the 2015 paper as closely as possible:

    * full schedule(m,t) Boolean matrix;
    * pairwise encoding for atMost(1);
    * commander-variable encoding (group size 3) for exactly(1);
    * cardinality networks for general exactly(k)/atMost(k);
    * exact fromSlot prefix semantics;
    * exact endHole reification;
    * a shared sorting network whose leading outputs are sortedHole;
    * the published one-way max/min approximation;
    * exact XOR for dif and hard atMost(2, dif);
    * implied constraints IC1 and IC2.

    Fixed-slot and precedence constraints are retained as dataset-side hard
    extensions because the repository benchmark includes those families.  They
    do not change the published objective encoding.  Precedence is encoded with
    the full-domain pairwise/direct construction used by the published-style
    baseline.

    This class is independent from B2BSATModel and from compact objective
    helpers.  Shared code with ORG_BG_D2 is limited to process/result utilities.
    """

    def __init__(self, instance: B2BInstance) -> None:
        self.inst = instance
        self.vpool = IDPool()
        self.cnf = CNF()
        self.x_vars: dict[tuple[int, int], int] = {}
        self.y_vars: dict[tuple[int, int], int] = {}
        self.from_slot_vars: dict[tuple[int, int], int] = {}
        self.end_hole_vars: dict[tuple[int, int], int] = {}
        self.sorted_hole_vars: list[list[int]] = [
            [] for _ in range(instance.n_business)
        ]
        self.max_vars: list[int] = []
        self.min_vars: list[int] = []
        self.dif_vars: list[int] = []
        self._aux_index = 0
        self._false_lit: int | None = None
        self._built = False

    def x(self, meeting: int, slot: int) -> int:
        return self.vpool.id(("schedule", meeting, slot))

    def y(self, participant: int, slot: int) -> int:
        return self.vpool.id(("usedSlot", participant, slot))

    def from_slot(self, participant: int, slot: int) -> int:
        return self.vpool.id(("fromSlot", participant, slot))

    def end_hole(self, participant: int, slot: int) -> int:
        return self.vpool.id(("endHole", participant, slot))

    def _aux(self, family: str) -> int:
        self._aux_index += 1
        return self.vpool.id(("paper_aux", family, self._aux_index))

    def _constant_false(self) -> int:
        if self._false_lit is None:
            self._false_lit = self._aux("false")
            self.cnf.append([-self._false_lit])
        return self._false_lit

    def _append_cardinality(self, encoding: Any) -> None:
        self.cnf.extend(encoding.clauses)

    def _at_most_one_pairwise(self, literals: list[int]) -> None:
        for left in range(len(literals)):
            for right in range(left + 1, len(literals)):
                self.cnf.append([-literals[left], -literals[right]])

    def _naive_exactly_one(self, literals: list[int]) -> None:
        if not literals:
            self.cnf.append([])
            return
        self.cnf.append(list(literals))
        self._at_most_one_pairwise(literals)

    def _link_commander_group(
        self,
        group: list[int],
        commander_literal: int,
    ) -> None:
        """Encode commander <-> exactly-one-active-group semantics."""
        self._at_most_one_pairwise(group)
        # commander -> at least one subordinate
        self.cnf.append([-commander_literal, *group])
        # every true subordinate -> commander
        for literal in group:
            self.cnf.append([commander_literal, -literal])

    def _exactly_one_commander(self, literals: list[int]) -> None:
        """Klieber-Kwon commander encoding with the paper's best group size 3."""
        if not literals:
            self.cnf.append([])
            return
        if len(literals) <= 5:
            self._naive_exactly_one(literals)
            return

        groups = [
            literals[index : index + 3]
            for index in range(0, len(literals), 3)
        ]

        # Klieber-Kwon note that with two top-level groups the second commander
        # can be the negation of the first.  Keep that optimization explicitly.
        if len(groups) == 2:
            commander = self._aux("commander")
            self._link_commander_group(groups[0], commander)
            self._link_commander_group(groups[1], -commander)
            return

        commanders: list[int] = []
        for group in groups:
            commander = self._aux("commander")
            self._link_commander_group(group, commander)
            commanders.append(commander)
        self._exactly_one_commander(commanders)

    def _at_most_cardinality(self, literals: list[int], bound: int) -> None:
        if bound < 0:
            self.cnf.append([])
        elif bound == 0:
            self.cnf.extend([[-literal] for literal in literals])
        elif bound == 1:
            self._at_most_one_pairwise(literals)
        elif bound < len(literals):
            self._append_cardinality(
                CardEnc.atmost(
                    lits=literals,
                    bound=bound,
                    vpool=self.vpool,
                    encoding=EncType.cardnetwrk,
                )
            )

    def _exactly_cardinality(self, literals: list[int], bound: int) -> None:
        if bound < 0 or bound > len(literals):
            self.cnf.append([])
        elif bound == 0:
            self.cnf.extend([[-literal] for literal in literals])
        elif bound == len(literals):
            self.cnf.extend([[literal] for literal in literals])
        elif bound == 1:
            self._exactly_one_commander(literals)
        else:
            self._append_cardinality(
                CardEnc.equals(
                    lits=literals,
                    bound=bound,
                    vpool=self.vpool,
                    encoding=EncType.cardnetwrk,
                )
            )

    def _comparator(self, left: int, right: int) -> tuple[int, int]:
        """Exact Boolean comparator, returning (OR, AND) = descending outputs."""
        high = self._aux("sort_high")
        low = self._aux("sort_low")

        # high <-> left OR right
        self.cnf.append([-left, high])
        self.cnf.append([-right, high])
        self.cnf.append([left, right, -high])

        # low <-> left AND right
        self.cnf.append([left, -low])
        self.cnf.append([right, -low])
        self.cnf.append([-left, -right, low])
        return high, low

    def _odd_even_merge(self, wires: list[int]) -> list[int]:
        if len(wires) == 1:
            return list(wires)
        if len(wires) == 2:
            high, low = self._comparator(wires[0], wires[1])
            return [high, low]

        odd = self._odd_even_merge(wires[0::2])
        even = self._odd_even_merge(wires[1::2])
        merged = [0] * len(wires)
        merged[0] = odd[0]
        merged[-1] = even[-1]
        for index in range(1, len(wires) - 1, 2):
            high, low = self._comparator(
                even[(index - 1) // 2],
                odd[(index + 1) // 2],
            )
            merged[index] = high
            merged[index + 1] = low
        return merged

    def _odd_even_sort(self, wires: list[int]) -> list[int]:
        if len(wires) <= 1:
            return list(wires)
        middle = len(wires) // 2
        left = self._odd_even_sort(wires[:middle])
        right = self._odd_even_sort(wires[middle:])
        return self._odd_even_merge(left + right)

    def _sorting_network(self, literals: list[int]) -> list[int]:
        if not literals:
            return []
        size = 1
        while size < len(literals):
            size *= 2
        padded = list(literals)
        if size > len(literals):
            padded.extend([self._constant_false()] * (size - len(literals)))
        return self._odd_even_sort(padded)

    def _build_hard_schedule(self) -> None:
        inst = self.inst

        for meeting in range(inst.n_meetings):
            for slot in range(inst.n_total_slots):
                self.x_vars[meeting, slot] = self.x(meeting, slot)
        for participant in range(inst.n_business):
            for slot in range(inst.n_total_slots):
                self.y_vars[participant, slot] = self.y(participant, slot)
                self.from_slot_vars[participant, slot] = self.from_slot(
                    participant,
                    slot,
                )

        # Paper Eq. (1): atMost(1) by pairwise mutex clauses.
        for participant, meetings in enumerate(inst.meetings_by_business):
            for slot in range(inst.n_total_slots):
                self._at_most_one_pairwise(
                    [self.x(meeting, slot) for meeting in meetings]
                )

        # Paper Eqs. (3)--(7): exactly one allowed slot, with commander encoding
        # for exactly(1), plus explicit unit clauses outside the requested session.
        for meeting, (_, _, session) in enumerate(inst.requested):
            if session == 1:
                allowed = list(range(inst.n_morning_slots))
            elif session == 2:
                allowed = list(range(inst.n_morning_slots, inst.n_total_slots))
            else:
                allowed = list(range(inst.n_total_slots))
            self._exactly_one_commander(
                [self.x(meeting, slot) for slot in allowed]
            )
            allowed_set = set(allowed)
            for slot in range(inst.n_total_slots):
                if slot not in allowed_set:
                    self.cnf.append([-self.x(meeting, slot)])

        # Paper Eq. (8): capacity by cardinality network.
        for slot in range(inst.n_total_slots):
            self._at_most_cardinality(
                [self.x(meeting, slot) for meeting in range(inst.n_meetings)],
                inst.n_tables,
            )

        # Dataset-side hard extensions retained from ORG_BG_D2.
        for meeting, fixed_slot in enumerate(inst.fixed):
            if fixed_slot is not None:
                self.cnf.append([self.x(meeting, fixed_slot)])

        # Paper Eq. (2): no meeting involving p in a forbidden slot.
        for participant, forbidden_slots in enumerate(inst.forbidden):
            for slot in forbidden_slots:
                for meeting in inst.meetings_by_business[participant]:
                    self.cnf.append([-self.x(meeting, slot)])

        # Published-style full-domain pairwise/direct precedence extension.
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

        # Paper Eqs. (9)--(10): schedule <-> usedSlot channeling.
        for participant, meetings in enumerate(inst.meetings_by_business):
            for slot in range(inst.n_total_slots):
                scheduled = [self.x(meeting, slot) for meeting in meetings]
                for literal in scheduled:
                    self.cnf.append([-literal, self.y(participant, slot)])
                self.cnf.append([-self.y(participant, slot), *scheduled])

        # Paper extended encoding IC1 (Eq. 22).
        for participant in range(inst.n_business):
            self._exactly_cardinality(
                [
                    self.y(participant, slot)
                    for slot in range(inst.n_total_slots)
                ],
                inst.n_meetings_business[participant],
            )

        # Paper extended encoding IC2 (Eq. 23).
        for slot in range(inst.n_total_slots):
            self._at_most_cardinality(
                [
                    self.y(participant, slot)
                    for participant in range(inst.n_business)
                ],
                2 * inst.n_tables,
            )

    def _build_published_objective(self) -> None:
        inst = self.inst

        # Paper Eqs. (11)--(14): exact fromSlot prefix semantics.
        for participant in range(inst.n_business):
            for slot in range(inst.n_total_slots):
                prefix = self.from_slot(participant, slot)
                used = self.y(participant, slot)
                if slot == 0:
                    # not used -> not fromSlot, and used -> fromSlot.
                    self.cnf.append([used, -prefix])
                    self.cnf.append([-used, prefix])
                else:
                    previous = self.from_slot(participant, slot - 1)
                    # (not previous and not used) -> not prefix.
                    self.cnf.append([previous, used, -prefix])
                    # used -> prefix.
                    self.cnf.append([-used, prefix])
                    # previous -> prefix.
                    self.cnf.append([-previous, prefix])

        # Paper Eq. (16): endHole iff an idle period ends at slot j.
        for participant in range(inst.n_business):
            ends: list[int] = []
            for slot in range(inst.n_total_slots - 1):
                end = self.end_hole(participant, slot)
                self.end_hole_vars[participant, slot] = end
                ends.append(end)
                current = self.y(participant, slot)
                following = self.y(participant, slot + 1)
                prefix = self.from_slot(participant, slot)

                # end <-> (!current & prefix & following)
                self.cnf.append([-end, -current])
                self.cnf.append([-end, prefix])
                self.cnf.append([-end, following])
                self.cnf.append([current, -prefix, -following, end])

            # Paper Eq. (17): sortingNetwork(endHole, sortedHole).  The paper's
            # cardinality-network variant keeps only the first floor((T-1)/2)
            # outputs because no participant can have more break groups.
            sorted_outputs = self._sorting_network(ends)
            upper = min(
                (inst.n_total_slots - 1) // 2,
                len(sorted_outputs),
            )
            self.sorted_hole_vars[participant] = sorted_outputs[:upper]

        # Paper Eqs. (18)--(21): one-way max/min approximation, exact XOR dif,
        # and hard d=2 bound.
        global_upper = (inst.n_total_slots - 1) // 2
        for amount in range(global_upper):
            maximum = self._aux("max")
            minimum = self._aux("min")
            difference = self._aux("dif")
            self.max_vars.append(maximum)
            self.min_vars.append(minimum)
            self.dif_vars.append(difference)

            for participant in range(inst.n_business):
                sorted_hole = self.sorted_hole_vars[participant][amount]
                # sortedHole[p,j] -> max[j]
                self.cnf.append([-sorted_hole, maximum])
                # not sortedHole[p,j] -> not min[j]  == min[j] -> sortedHole[p,j]
                self.cnf.append([sorted_hole, -minimum])

            # difference <-> (minimum XOR maximum)
            self.cnf.append([minimum, maximum, -difference])
            self.cnf.append([minimum, -maximum, difference])
            self.cnf.append([-minimum, maximum, difference])
            self.cnf.append([-minimum, -maximum, -difference])

        self._at_most_cardinality(self.dif_vars, PAPER_FAIRNESS_D)

    def build(self) -> None:
        if self._built:
            return
        self._build_hard_schedule()
        self._build_published_objective()
        self._built = True

    def build_wcnf(self) -> WCNF:
        self.build()
        formula = WCNF()
        for clause in self.cnf.clauses:
            formula.append(clause)

        # Paper cardinality-network variant: soft not sortedHole[p,j], weight 1.
        for participant_outputs in self.sorted_hole_vars:
            for literal in participant_outputs:
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
            for participant_outputs in self.sorted_hole_vars
            for literal in participant_outputs
        )
        if encoded_groups != metrics.total_break_groups:
            errors.append(
                "published sortedHole mismatch: "
                f"encoded={encoded_groups}, decoded={metrics.total_break_groups}"
            )
        if solver_cost != metrics.total_break_groups:
            errors.append(
                f"solver-cost mismatch: {solver_cost}!={metrics.total_break_groups}"
            )
        if metrics.break_group_range > PAPER_FAIRNESS_D:
            errors.append(
                "published homogeneity cap violated: "
                f"{metrics.break_group_range}>{PAPER_FAIRNESS_D}"
            )
        return assignment, metrics, errors


def _solve_formula(
    baseline: PaperExactBGBaseline,
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
    with tempfile.NamedTemporaryFile(prefix="org_paper_", suffix=".wcnf") as stream:
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
    baseline = PaperExactBGBaseline(instance)
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
    soft_count = sum(len(values) for values in baseline.sorted_hole_vars)
    objective_vector = (
        (metrics.total_break_groups,) if metrics is not None else None
    )

    return {
        "configuration_label": PAPER_CONFIGURATION_LABEL,
        "configuration_id": PAPER_CONFIGURATION_ID,
        "configuration_key": PAPER_CONFIGURATION_ID,
        "factor_m": "ORGFull",
        "factor_f": "N/A",
        "factor_p": "Pairwise",
        "factor_g": "Direct-E",
        "factor_b": "CardinalityNetwork",
        "factor_o": "BreakGroupsD2",
        "factor_s": solver_backend,
        "factor_i": PAPER_IMPLIED_PACKAGE_NAME,
        "formalism": "MaxSAT",
        "model_family": "ORGPublishedPaper",
        "formulation_name": "ORG-Published-Cardinality-IC12-BG-d2",
        "model_family_display_name": "Published-paper-style",
        "model_configuration_display_name": "Published-paper-style/Full",
        "implementation_provenance": "independent_reimplementation_from_published_equations",
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
        "encoding_variant": PAPER_ENCODING_VARIANT,
        "idle_encoding": "endhole_sorting_network",
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
        "implied_constraints_code": PAPER_IMPLIED_PACKAGE_CODE,
        "status": status,
        "sat_result": "SAT" if status == "OPTIMAL" else status,
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
        "n_bound_encodings": 1,
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
        # Audit fields.  write_detailed_csv may omit them from fixed schemas,
        # but direct solve_instance callers can inspect them.
        "paper_exactly_one_encoding": PAPER_EXACTLY_ONE_ENCODING,
        "paper_general_cardinality_encoding": PAPER_GENERAL_CARDINALITY_ENCODING,
        "paper_sorting_network_encoding": PAPER_SORTING_NETWORK_ENCODING,
        "paper_max_min_encoding": PAPER_MAX_MIN_ENCODING,
        "paper_difference_encoding": PAPER_DIFFERENCE_ENCODING,
        "paper_fairness_d": PAPER_FAIRNESS_D,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the paper-faithful ORG/full MaxSAT BG-d2 baseline "
            "(cardinality-network + IC12)."
        )
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
    parser.add_argument(
        "--backend",
        choices=["uwrmaxsat", "rc2"],
        default="uwrmaxsat",
    )
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
    excel_dir = Path(
        args.excel_dir or output_path.parent / "excel_org_paper_exact"
    )
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
