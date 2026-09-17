from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import Any

from pysat.card import CardEnc, EncType

from B2B_Instance import (
    LEXICOGRAPHIC_MODES,
    B2BInstance,
    B2BSATModel,
    B2BSolutionStats,
    read_instance,
)


def _ensure_instance(instance_or_path: B2BInstance | str | Path) -> B2BInstance:
    return (
        instance_or_path
        if isinstance(instance_or_path, B2BInstance)
        else read_instance(instance_or_path)
    )


def _new_solver(clauses: list[list[int]], preferred: str = "cadical"):
    """Create a SAT solver, falling back to Glucose3 when CaDiCaL is unavailable."""
    solvers = import_module("pysat.solvers")
    if preferred == "glucose":
        return solvers.Glucose3(bootstrap_with=clauses)
    try:
        return solvers.Cadical153(bootstrap_with=clauses)
    except Exception:
        return solvers.Glucose3(bootstrap_with=clauses)


class B2BMultipleSATSolver:
    """Repeated-SAT lexicographic optimization in IdleRange, IdleMax, IdleSum order.

    Every candidate bound is solved in a fresh SAT solver. B(p) counts idle slots
    strictly between participant p's first and last meetings, and P* contains the
    participants with at least two meetings. The three tiers are

        phase 1  IR = max_{p in P*} B(p) - min_{p in P*} B(p)
        phase 2  IM = max_{p in P*} B(p)
        phase 3  IS = sum_{p in P*} B(p)

    Each phase folds its proven optimum into ``_hard_clauses`` as a cardinality
    bound, so every later phase bootstraps from a clause set that already pins the
    tiers above it; nothing is re-encoded per phase. Note MaxSAT_Solver puts IM
    first instead, in a single weighted RC2 call.
    """

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        fairness_limit: int | None = None,
        precedence_mode: str = "traditional",
        encoding_variant: str = "imp12+",
        solver_name: str = "cadical",
        objective_mode: str = "im-is",
        precedence_edge_mode: str = "direct",
    ) -> None:
        self.inst = _ensure_instance(instance_or_path)
        self.model = B2BSATModel(
            inst=self.inst,
            fairness_limit=fairness_limit,
            precedence_mode=precedence_mode,
            encoding_variant=encoding_variant,
            objective_mode=objective_mode,
            precedence_edge_mode=precedence_edge_mode,
        )
        self.artifacts = self.model.build_base_cnf()
        self.solver_name = solver_name
        # Hard clause set carried between phases. Phase 1 starts from the base
        # CNF; each proven optimum is appended to it as a cardinality bound.
        self._hard_clauses: list[list[int]] = list(self.artifacts.cnf.clauses)
        self._hard_top = self.artifacts.n_vars

    # Objective tiers ---------------------------------------------------

    @property
    def _range_lits(self) -> list[int]:
        """IR tier: true count is max_{p in P*} B(p) - min_{p in P*} B(p)."""
        return self.artifacts.fairness_gap_lits

    @property
    def _max_lits(self) -> list[int]:
        """IM tier: true count is max_{p in P*} B(p)."""
        return self.artifacts.objective_lits

    def _pin_tier(self, lits: list[int], optimum: int) -> None:
        """Append ``sum(lits) <= optimum`` to the hard clauses for later phases.

        Encoded once, when the tier's optimum is proven, rather than rebuilt by
        every phase below it. ``_hard_top`` keeps the auxiliary variables of
        successive bounds disjoint.
        """
        clauses, self._hard_top = self._cardinality_bound(
            lits,
            optimum,
            self._hard_top,
        )
        self._hard_clauses.extend(clauses)

    def _pack_result(
        self,
        status: str,
        assignment: list[int] | None,
        stats: B2BSolutionStats | None,
        checks: list[str] | None = None,
        *,
        proven_optimum: int | None = None,
        secondary_optimum: int | None = None,
        tertiary_optimum: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "solver": "MultipleSAT",
            "precedence_mode": self.artifacts.precedence_mode,
            "precedence_edge_mode": self.artifacts.precedence_edge_mode,
            "encoding_variant": self.artifacts.encoding_variant,
            # artifacts.objective_name describes the MaxSAT tier order (IM
            # first); this solver leads with IdleRange instead.
            "objective": "lexicographic_idle_range_then_idle_max_then_idle_sum",
            "objective_mode": self.artifacts.objective_mode,
            "objective_participant_count": len(
                self.artifacts.objective_participants
            ),
            "objective_participants": tuple(
                p + 1 for p in self.artifacts.objective_participants
            ),
            "objective_value": (
                stats.fairness_gap if stats is not None else proven_optimum
            ),
            "proven_optimum": proven_optimum,
            # secondary tier is IdleMax = max_p B(p).
            "secondary_objective_value": (
                stats.max_internal_idle_slots if stats is not None else None
            ),
            "secondary_proven_optimum": secondary_optimum,
            "tertiary_objective_value": (
                stats.total_internal_idle_slots
                if stats is not None
                and self.artifacts.objective_mode in LEXICOGRAPHIC_MODES
                else None
            ),
            "tertiary_proven_optimum": tertiary_optimum,
            "hard_fairness_limit": self.artifacts.fairness_limit,
            "assignment": assignment,
            "stats": stats,
            "validation_errors": checks or [],
            "n_vars": self.artifacts.n_vars,
            "n_clauses": self.artifacts.n_clauses,
            "n_objective_lits": len(self._range_lits),
            "n_primary_objective_lits": len(self._range_lits),
            "n_secondary_objective_lits": len(self._max_lits),
            "precedence_direct_edges": self.artifacts.precedence_direct_edges,
            "precedence_source_added_edges": (
                self.artifacts.precedence_source_added_edges
            ),
            "precedence_encoded_edges": self.artifacts.precedence_encoded_edges,
            "precedence_full_closure_edges": (
                self.artifacts.precedence_transitive_edges
            ),
            "enabled_constraints": self.artifacts.enabled_constraints,
        }

    def _count_true(self, lits: list[int], model: list[int]) -> int:
        return sum(
            1
            for lit in lits
            if model[abs(lit) - 1] * (1 if lit > 0 else -1) > 0
        )

    def _range_checks(
        self,
        sat_model: list[int],
        stats: B2BSolutionStats,
        *,
        imposed_bound: int | None = None,
    ) -> list[str]:
        """Cross-check the encoded IdleRange against the decoded schedule."""
        encoded = self._count_true(self._range_lits, sat_model)
        errors: list[str] = []
        if encoded != stats.fairness_gap:
            errors.append(
                "IdleRange encoding mismatch: "
                f"encoded IR={encoded}, schedule IR={stats.fairness_gap}"
            )
        if imposed_bound is not None and encoded > imposed_bound:
            errors.append(
                f"IdleRange-bound violation: encoded IR={encoded}, "
                f"bound={imposed_bound}"
            )
        return errors

    def _evaluate_sat_model(
        self,
        sat_model: list[int],
        *,
        range_bound: int | None = None,
        max_bound: int | None = None,
    ) -> tuple[list[int], B2BSolutionStats, list[str]]:
        """Decode a model and check both upper tiers against the schedule.

        The encoding-vs-schedule checks always run; ``range_bound`` and
        ``max_bound`` additionally assert the bound the current phase imposed.
        """
        assignment = self.model.decode_assignment(sat_model)
        stats = self.model.compute_stats(assignment)
        checks = self.model.validate_assignment(assignment)
        checks.extend(
            self._range_checks(sat_model, stats, imposed_bound=range_bound)
        )
        checks.extend(
            self.model.objective_consistency_errors(
                sat_model,
                stats,
                imposed_bound=max_bound,
            )
        )
        return assignment, stats, checks

    @staticmethod
    def _cardinality_bound(
        lits: list[int],
        bound: int,
        top_id: int,
    ) -> tuple[list[list[int]], int]:
        """Encode ``sum(lits) <= bound`` and return clauses plus the new top id."""
        if bound < 0:
            return [[]], top_id
        if not lits or bound >= len(lits):
            return [], top_id
        if bound == 0:
            return [[-lit] for lit in lits], top_id

        encoding = CardEnc.atmost(
            lits=lits,
            bound=bound,
            top_id=top_id,
            encoding=EncType.seqcounter,
        )
        return encoding.clauses, encoding.nv

    @staticmethod
    def _cardinality_atleast(
        lits: list[int],
        bound: int,
        top_id: int,
    ) -> tuple[list[list[int]], int]:
        """Encode ``sum(lits) >= bound`` and return clauses plus the new top id."""
        if bound <= 0:
            return [], top_id
        if bound > len(lits):
            return [[]], top_id
        if bound == len(lits):
            return [[lit] for lit in lits], top_id
        encoding = CardEnc.atleast(
            lits=lits,
            bound=bound,
            top_id=top_id,
            encoding=EncType.seqcounter,
        )
        return encoding.clauses, encoding.nv

    def _bound_clauses(self, bound: int) -> list[list[int]]:
        """Encode the phase-1 IdleRange bound for one fresh SAT run."""
        clauses, _ = self._cardinality_bound(
            self._range_lits,
            bound,
            self._hard_top,
        )
        return clauses

    def _optimize_secondary(
        self,
        range_optimum: int,
        *,
        verbose: bool = False,
    ) -> tuple[list[int] | None, B2BSolutionStats | None, list[str], int | None]:
        """Minimize IdleMax with the proven IdleRange already hard.

        ``_hard_clauses`` carries IR <= R* from phase 1, so this phase only has
        to encode its own candidate bounds. With IR pinned at R*, minimizing
        IdleMax also minimizes IdleMin = IM - R*, and the proven pair fixes
        IdleMin exactly for phase 3.
        """
        phase2_base = self._hard_clauses

        with _new_solver(phase2_base, self.solver_name) as solver:
            if not solver.solve():
                return None, None, [
                    "phase 2 became UNSAT after fixing the phase-1 optimum"
                ], None
            initial_model = solver.get_model()

        assignment, stats, checks = self._evaluate_sat_model(
            initial_model,
            range_bound=range_optimum,
        )
        if checks:
            return assignment, stats, checks, None

        best_assignment = assignment
        best_stats = stats
        max_lits = self._max_lits
        best_max = stats.max_internal_idle_slots

        # B(p) >= 0 for every p, so
        #   max_p B(p) >= max_p B(p) - min_p B(p) = IdleRange,
        # and IdleRange is pinned at R*, making R* a proven lower bound on
        # IdleMax. The incumbent above is satisfiable, so the optimum lies in
        # [R*, best_max].
        level_cap = min(self.inst.n_total_slots, len(max_lits))
        lower = range_optimum
        upper = min(best_max, level_cap)

        if verbose:
            print(
                f"[MultipleSAT] phase-2 IdleMax in [{lower}, {upper}] "
                f"(incumbent={best_max})"
            )
        if lower >= best_max:
            # The incumbent already sits on the lower bound, so it is optimal.
            return best_assignment, best_stats, [], best_max

        # Binary search on [lower, upper]. lower is a proven lower bound
        # (IdleMax >= IdleRange) and upper is the incumbent, known satisfiable,
        # so lo == hi is the optimum.
        lo, hi = lower, upper
        while lo < hi:
            bound = (lo + hi) // 2
            max_clauses, _ = self._cardinality_bound(
                max_lits,
                bound,
                self._hard_top,
            )
            with _new_solver(phase2_base, self.solver_name) as solver:
                solver.append_formula(max_clauses)
                sat = solver.solve()
                sat_model = solver.get_model() if sat else None

            if verbose:
                print(
                    f"[MultipleSAT] IdleMax <= {bound}: "
                    f"{'SAT' if sat else 'UNSAT'}"
                )
            if sat_model is None:
                # Unreachable at this level, so the optimum is above it.
                lo = bound + 1
                continue

            assignment, stats, candidate_checks = self._evaluate_sat_model(
                sat_model,
                range_bound=range_optimum,
                max_bound=bound,
            )
            if candidate_checks:
                return assignment, stats, candidate_checks, None
            best_assignment = assignment
            best_stats = stats
            hi = bound
        optimum = lo

        final_checks = self.model.validate_assignment(best_assignment)
        if best_stats.fairness_gap != range_optimum:
            final_checks.append(
                "lexicographic primary mismatch: "
                f"proven IdleRange={range_optimum}, "
                f"schedule IdleRange={best_stats.fairness_gap}"
            )
        if best_stats.max_internal_idle_slots != optimum:
            final_checks.append(
                "lexicographic secondary mismatch: "
                f"proven IdleMax={optimum}, "
                f"schedule IdleMax={best_stats.max_internal_idle_slots}"
            )
        return best_assignment, best_stats, final_checks, optimum

    def _optimize_tertiary(
        self,
        range_optimum: int,
        max_optimum: int | None,
        *,
        verbose: bool = False,
    ) -> tuple[list[int] | None, B2BSolutionStats | None, list[str], int | None]:
        """Minimize IdleSum with the proven upper tiers already hard.

        ``_hard_clauses`` carries IR <= R* and IM <= M* from phases 1 and 2, so
        this phase adds only the bounds it derives from that pair.
        ``max_optimum`` is None when the IdleMax tier is skipped, leaving IR the
        only pinned tier.

        Binary search from a proven lower bound: each satisfiable bound yields a
        model whose actual sum usually undercuts the requested bound by a wide
        margin, so this converges in a few SAT calls over the 1334 IdleSum
        literals.
        """
        minimum_clauses: list[list[int]] = []
        bound_clauses: list[list[int]] = []
        sum_lower = 0
        derived_top = self._hard_top
        if max_optimum is not None:
            # Both upper tiers are pinned exactly: the hard bounds give
            # IR <= R* and IM <= M*, while R* and M* being proven optima give
            # IR >= R* and IM >= M*. IR = IdleMax - IdleMin then fixes
            # IdleMin = M* - R*. Bounding it adds no solutions, but hands the
            # solver a constraint that propagates directly into the schedule.
            idle_min = max_optimum - range_optimum
            minimum_clauses, derived_top = self._cardinality_bound(
                self.model.min_break_lits(),
                idle_min,
                derived_top,
            )

            # Lower bound on IdleSum. With IdleMin = M* - R*, every one of the
            # |P*| objective participants has B(p) >= IdleMin and at least one
            # reaches IdleMax, so
            #     IdleSum >= IdleMin * (|P*| - 1) + IdleMax.
            # The multiplier is |P*|, not nBusiness: participants with fewer
            # than two meetings have B(p) = 0 and cannot contribute IdleMin, so
            # using nBusiness would overshoot and cut off real optima whenever
            # IdleMin > 0.
            # Set B2B_SUM_BOUNDS=0 to disable both IdleSum bounds, for A/B
            # measurement of whether they actually pay for their encoding cost.
            if os.environ.get("B2B_SUM_BOUNDS", "1") != "0":
                n_star = len(self.artifacts.objective_participants)
                sum_lower = idle_min * max(0, n_star - 1) + max_optimum
                lower_clauses, derived_top = self._cardinality_atleast(
                    self.artifacts.secondary_objective_lits,
                    sum_lower,
                    derived_top,
                )
                bound_clauses.extend(lower_clauses)

                # Upper bound, the mirror image: every objective participant has
                # B(p) <= IdleMax and at least one sits at IdleMin, so
                #     IdleSum <= IdleMax * (|P*| - 1) + IdleMin.
                sum_upper = max_optimum * max(0, n_star - 1) + idle_min
                upper_clauses, derived_top = self._cardinality_bound(
                    self.artifacts.secondary_objective_lits,
                    sum_upper,
                    derived_top,
                )
                bound_clauses.extend(upper_clauses)

        phase3_base = [
            *self._hard_clauses,
            *minimum_clauses,
            *bound_clauses,
        ]

        with _new_solver(phase3_base, self.solver_name) as solver:
            if not solver.solve():
                return None, None, [
                    "phase 3 became UNSAT after fixing the phase-2 optimum"
                ], None
            model = solver.get_model()

        best_assignment, best_stats, checks = self._evaluate_sat_model(
            model,
            range_bound=range_optimum,
            max_bound=max_optimum,
        )
        checks.extend(
            self.model.secondary_objective_consistency_errors(model, best_stats)
        )
        if checks:
            return best_assignment, best_stats, checks, None

        sum_lits = self.artifacts.secondary_objective_lits
        best_sum = best_stats.total_internal_idle_slots
        if verbose:
            print(f"[MultipleSAT] phase-3 initial IdleSum={best_sum}")

        # Binary search on [sum_lower, best_sum]. sum_lower is the proven lower
        # bound IdleMin*(|P*|-1) + IdleMax (0 when there is no IdleMax level),
        # and best_sum is the incumbent, known satisfiable.
        lo, hi = (sum_lower, best_sum) if sum_lits else (best_sum, best_sum)
        while lo < hi:
            bound = (lo + hi) // 2
            sum_clauses, _ = self._cardinality_bound(
                sum_lits,
                bound,
                derived_top,
            )
            with _new_solver(phase3_base, self.solver_name) as solver:
                solver.append_formula(sum_clauses)
                sat = solver.solve()
                model = solver.get_model() if sat else None

            if verbose:
                print(
                    f"[MultipleSAT] IdleSum <= {bound}: "
                    f"{'SAT' if sat else 'UNSAT'}"
                )
            if model is None:
                # Unreachable at this level, so the optimum is above it.
                lo = bound + 1
                continue

            assignment, stats, candidate_checks = self._evaluate_sat_model(
                model,
                range_bound=range_optimum,
                max_bound=max_optimum,
            )
            candidate_checks.extend(
                self.model.secondary_objective_consistency_errors(
                    model,
                    stats,
                    imposed_bound=bound,
                )
            )
            if candidate_checks:
                return assignment, stats, candidate_checks, None
            best_assignment = assignment
            best_stats = stats
            hi = bound
        best_sum = lo

        final_checks = self.model.validate_assignment(best_assignment)
        if best_stats.fairness_gap != range_optimum:
            final_checks.append(
                "lexicographic primary mismatch: "
                f"proven IdleRange={range_optimum}, "
                f"schedule IdleRange={best_stats.fairness_gap}"
            )
        if (
            max_optimum is not None
            and best_stats.max_internal_idle_slots != max_optimum
        ):
            final_checks.append(
                "lexicographic secondary mismatch: "
                f"proven IdleMax={max_optimum}, "
                f"schedule IdleMax={best_stats.max_internal_idle_slots}"
            )
        return best_assignment, best_stats, final_checks, best_sum

    def solve(self, verbose: bool = False) -> dict[str, Any]:
        with _new_solver(self._hard_clauses, self.solver_name) as solver:
            if not solver.solve():
                return self._pack_result("UNSAT", None, None)
            initial_model = solver.get_model()

        best_assignment, best_stats, checks = self._evaluate_sat_model(initial_model)
        if checks:
            return self._pack_result("ERROR", best_assignment, best_stats, checks)

        best_obj = best_stats.fairness_gap
        if verbose:
            print(f"[MultipleSAT] initial IdleRange(P*)={best_obj}")

        low, high = 0, best_obj - 1
        while low <= high:
            bound = (low + high) // 2
            bound_clauses = self._bound_clauses(bound)

            with _new_solver(self._hard_clauses, self.solver_name) as solver:
                solver.append_formula(bound_clauses)
                sat = solver.solve()
                if verbose:
                    print(
                        "[MultipleSAT] IdleRange(P*) <= "
                        f"{bound}: {'SAT' if sat else 'UNSAT'}"
                    )

                if sat:
                    candidate_model = solver.get_model()
                    (
                        candidate_assignment,
                        candidate_stats,
                        candidate_checks,
                    ) = self._evaluate_sat_model(
                        candidate_model,
                        range_bound=bound,
                    )
                    if candidate_checks:
                        return self._pack_result(
                            "ERROR",
                            candidate_assignment,
                            candidate_stats,
                            candidate_checks,
                        )

                    best_assignment = candidate_assignment
                    best_stats = candidate_stats
                    high = bound - 1
                else:
                    low = bound + 1

        final_checks = self.model.validate_assignment(best_assignment)
        if best_stats.fairness_gap != low:
            final_checks.append(
                "optimization mismatch: "
                f"proven IdleRange={low}, "
                f"schedule IdleRange={best_stats.fairness_gap}"
            )

        if self.artifacts.objective_mode in LEXICOGRAPHIC_MODES and not final_checks:
            # IR is proven: fold IR <= R* into the hard clauses so phase 2
            # searches IdleMax over a formula that already pins the tier above.
            self._pin_tier(self._range_lits, low)

            secondary_optimum = None
            skip_max = False  # phase 2 proves IdleMax before the IdleSum tier
            if not skip_max:
                (
                    best_assignment,
                    best_stats,
                    final_checks,
                    secondary_optimum,
                ) = self._optimize_secondary(low, verbose=verbose)

            tertiary_optimum = None
            if not final_checks and (skip_max or secondary_optimum is not None):
                # IM is proven too: pin it as well, so phase 3 searches IdleSum
                # with both IR <= R* and IM <= M* hard.
                if secondary_optimum is not None:
                    self._pin_tier(self._max_lits, secondary_optimum)
                (
                    best_assignment,
                    best_stats,
                    final_checks,
                    tertiary_optimum,
                ) = self._optimize_tertiary(
                    low,
                    secondary_optimum,
                    verbose=verbose,
                )

            status = "OPTIMAL" if not final_checks else "ERROR"
            return self._pack_result(
                status,
                best_assignment,
                best_stats,
                final_checks,
                proven_optimum=low,
                secondary_optimum=secondary_optimum,
                tertiary_optimum=tertiary_optimum,
            )

        status = "OPTIMAL" if not final_checks else "ERROR"
        return self._pack_result(
            status,
            best_assignment,
            best_stats,
            final_checks,
            proven_optimum=low,
        )


def solve_b2b(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    precedence_mode: str = "traditional",
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    objective_mode: str = "im-is",
    precedence_edge_mode: str = "direct",
) -> dict[str, Any]:
    return B2BMultipleSATSolver(
        instance_or_path=instance_or_path,
        fairness_limit=fairness_limit,
        precedence_mode=precedence_mode,
        encoding_variant=encoding_variant,
        objective_mode=objective_mode,
        precedence_edge_mode=precedence_edge_mode,
    ).solve(verbose=verbose)


def solve_b2b_traditional(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    objective_mode: str = "im-is",
    precedence_edge_mode: str = "direct",
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path,
        fairness_limit,
        "traditional",
        encoding_variant,
        verbose,
        objective_mode,
        precedence_edge_mode,
    )


def solve_b2b_staircase(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    objective_mode: str = "im-is",
    precedence_edge_mode: str = "direct",
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path,
        fairness_limit,
        "staircase",
        encoding_variant,
        verbose,
        objective_mode,
        precedence_edge_mode,
    )
