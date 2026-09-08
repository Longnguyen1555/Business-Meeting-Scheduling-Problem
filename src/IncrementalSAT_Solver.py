from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import Any

from pysat.card import CardEnc, EncType, ITotalizer

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


class B2BIncrementalSATSolver:
    """Incremental SAT optimization of the internal-idle-slot range over P*.

    For participant p, B(p) is the number of idle slots strictly between p's
    first and last meetings. The optimized objective is::

        max_{p in P*} B(p) - min_{p in P*} B(p),

    where P* contains participants with at least two meetings.

    Within each optimization phase, one SAT solver is retained while an
    incremental totalizer imposes candidate upper bounds through assumptions.
    The optional second phase fixes the range optimum and minimizes the
    bottleneck max_{p in P*} B(p). Note this differs from MaxSAT_Solver, whose
    second phase minimizes the sum of B(p).
    """

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        fairness_limit: int | None = None,
        precedence_mode: str = "traditional",
        encoding_variant: str = "imp12+",
        solver_name: str = "cadical",
        objective_mode: str = "idle-range",
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
            "solver": "IncrementalSAT",
            "precedence_mode": self.artifacts.precedence_mode,
            "precedence_edge_mode": self.artifacts.precedence_edge_mode,
            "encoding_variant": self.artifacts.encoding_variant,
            "objective": self.artifacts.objective_name,
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
            "secondary_objective_value": (
                self._secondary_value(stats)
                if stats is not None
                and self.artifacts.objective_mode == "lexicographic"
                else None
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
            "n_objective_lits": len(self.artifacts.objective_lits),
            "n_primary_objective_lits": len(self.artifacts.objective_lits),
            "n_secondary_objective_lits": len(self._secondary_lits()),
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

    def _evaluate_sat_model(
        self,
        sat_model: list[int],
        *,
        imposed_bound: int | None = None,
    ) -> tuple[list[int], B2BSolutionStats, list[str]]:
        assignment = self.model.decode_assignment(sat_model)
        stats = self.model.compute_stats(assignment)
        checks = self.model.validate_assignment(assignment)
        checks.extend(
            self.model.objective_consistency_errors(
                sat_model,
                stats,
                imposed_bound=imposed_bound,
            )
        )
        return assignment, stats, checks

    @staticmethod
    def _cardinality_bound(
        lits: list[int],
        bound: int,
        top_id: int,
    ) -> tuple[list[list[int]], int]:
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

    def _count_true(self, lits: list[int], model: list[int]) -> int:
        return sum(
            1
            for lit in lits
            if model[abs(lit) - 1] * (1 if lit > 0 else -1) > 0
        )

    def _secondary_lits(self) -> list[int]:
        """Unary literals whose true count is max_{p in P*} I_p(S)."""
        return self.model.max_break_lits()

    def _secondary_value(self, stats: B2BSolutionStats) -> int:
        """max_p I_p(S) read off the decoded schedule."""
        return stats.max_internal_idle_slots

    def _secondary_checks(
        self,
        sat_model: list[int],
        stats: B2BSolutionStats,
        *,
        imposed_bound: int | None = None,
    ) -> list[str]:
        """Cross-check the encoded max against the decoded schedule."""
        return self.model.secondary_max_consistency_errors(
            sat_model,
            stats,
            imposed_bound=imposed_bound,
        )

    def _optimize_secondary(
        self,
        primary_optimum: int,
        primary_model: list[int] | None = None,
        *,
        verbose: bool = False,
    ) -> tuple[list[int] | None, B2BSolutionStats | None, list[str], int | None]:
        """Incrementally minimize max_p I_p(S) with the optimal range fixed as hard."""
        primary_clauses, primary_top = self._cardinality_bound(
            self.artifacts.objective_lits,
            primary_optimum,
            self.artifacts.n_vars,
        )
        phase2_base = [*self.artifacts.cnf.clauses, *primary_clauses]

        with _new_solver(phase2_base, self.solver_name) as solver:
            if not solver.solve():
                return None, None, [
                    "phase 2 became UNSAT after fixing the phase-1 optimum"
                ], None

            initial_model = solver.get_model()
            best_assignment, best_stats, checks = self._evaluate_sat_model(
                initial_model,
                imposed_bound=primary_optimum,
            )
            checks.extend(self._secondary_checks(initial_model, best_stats))
            if checks:
                return best_assignment, best_stats, checks, None

            secondary_lits = self._secondary_lits()
            best_sat_model = initial_model
            best_max = self._count_true(secondary_lits, initial_model)

            # The phase-1 optimum model also satisfies phase2_base, so its own
            # max_p I_p(S) is a valid upper bound. Keep whichever of the two is
            # tighter: it caps the scan and shrinks the totalizer encoding.
            if primary_model is not None:
                primary_max = self._count_true(secondary_lits, primary_model)
                if primary_max < best_max:
                    (
                        seeded_assignment,
                        seeded_stats,
                        seeded_checks,
                    ) = self._evaluate_sat_model(
                        primary_model,
                        imposed_bound=primary_optimum,
                    )
                    if not seeded_checks:
                        best_assignment = seeded_assignment
                        best_stats = seeded_stats
                        best_sat_model = primary_model
                        best_max = primary_max

            # I_p(S) >= 0 for every p, so
            #   max_p I_p(S) >= max_p I_p(S) - min_p I_p(S) = IdleRange,
            # making the phase-1 optimum a valid lower bound on IdleMax. Scan
            # levels upward from there: the first satisfiable level IS the
            # optimum, so no bound above it is ever probed. The scan stops at
            # the incumbent, which is guaranteed satisfiable, and never exceeds
            # the number of threshold literals (a level the totalizer cannot
            # represent, and which n_total_slots can otherwise overshoot).
            level_cap = min(self.inst.n_total_slots, len(secondary_lits))
            lower = max(0, min(primary_optimum, level_cap))
            upper = min(best_max, level_cap)

            if verbose:
                print(
                    f"[IncrementalSAT] phase-2 IdleMax in [{lower}, {upper}] "
                    f"(incumbent={best_max})"
                )
            if lower >= best_max:
                # The incumbent already sits on the lower bound, so it is optimal.
                return best_assignment, best_stats, [], best_max

            optimum = best_max
            with ITotalizer(
                lits=secondary_lits,
                ubound=upper,
                top_id=primary_top,
            ) as totalizer:
                solver.append_formula(totalizer.cnf.clauses)

                # Binary search on [lower, upper]. lower is a proven lower bound
                # (IdleMax >= IdleRange) and upper is the incumbent, which is
                # known satisfiable, so the invariant "answer lies in [lo, hi]"
                # holds throughout and lo == hi is the optimum.
                lo, hi = lower, upper
                while lo < hi:
                    bound = (lo + hi) // 2
                    sat = solver.solve(assumptions=[-totalizer.rhs[bound]])
                    if verbose:
                        print(
                            f"[IncrementalSAT] IdleMax <= {bound}: "
                            f"{'SAT' if sat else 'UNSAT'}"
                        )
                    if not sat:
                        # Unreachable at this level, so the optimum is above it.
                        lo = bound + 1
                        continue

                    sat_model = solver.get_model()
                    (
                        candidate_assignment,
                        candidate_stats,
                        candidate_checks,
                    ) = self._evaluate_sat_model(
                        sat_model,
                        imposed_bound=primary_optimum,
                    )
                    candidate_checks.extend(
                        self._secondary_checks(
                            sat_model,
                            candidate_stats,
                            imposed_bound=bound,
                        )
                    )
                    if not candidate_checks:
                        best_assignment = candidate_assignment
                        best_stats = candidate_stats
                        best_sat_model = sat_model
                    hi = bound
                optimum = lo

        final_checks = self.model.validate_assignment(best_assignment)
        if best_stats.fairness_gap != primary_optimum:
            final_checks.append(
                "lexicographic primary mismatch: "
                f"proven range={primary_optimum}, "
                f"schedule range={best_stats.fairness_gap}"
            )
        schedule_max = self._count_true(secondary_lits, best_sat_model)
        if schedule_max != optimum:
            final_checks.append(
                "lexicographic secondary mismatch: "
                f"proven IdleMax={optimum}, "
                f"schedule IdleMax={schedule_max}"
            )
        return best_assignment, best_stats, final_checks, optimum

    def _optimize_tertiary(
        self,
        primary_optimum: int,
        secondary_optimum: int | None,
        *,
        verbose: bool = False,
    ) -> tuple[list[int] | None, B2BSolutionStats | None, list[str], int | None]:
        """Minimize IdleSum with the proven upper levels fixed as hard.

        ``secondary_optimum`` is None in ``lex-idlesum`` mode, where the IdleMax
        level is absent and only IdleRange is pinned.

        Unlike phases 1 and 2 this uses solution-improving descent rather than a
        scan: each satisfiable bound yields a model whose actual sum is usually
        far below the bound that was requested, so descending from the incumbent
        converges in a few cheap SAT calls and pays for exactly one UNSAT proof
        at the end. A scan or binary search over the 1334 IdleSum literals would
        instead pay for several expensive mid-range UNSAT proofs.
        """
        primary_clauses, primary_top = self._cardinality_bound(
            self.artifacts.objective_lits,
            primary_optimum,
            self.artifacts.n_vars,
        )
        minimum_clauses: list[list[int]] = []
        bound_clauses: list[list[int]] = []
        sum_lower = 0
        if secondary_optimum is None:
            secondary_clauses, secondary_top = [], primary_top
        else:
            secondary_clauses, secondary_top = self._cardinality_bound(
                self.model.max_break_lits(),
                secondary_optimum,
                primary_top,
            )
            # Both upper levels are now pinned exactly: the hard bounds give
            # range <= R* and max <= M*, while R* and M* being proven optima give
            # range >= R* and max >= M*. IdleRange = IdleMax - IdleMin then fixes
            # IdleMin = M* - R*. Bounding it adds no solutions, but hands the
            # solver a constraint that propagates directly into the schedule.
            minimum_clauses, secondary_top = self._cardinality_bound(
                self.model.min_break_lits(),
                secondary_optimum - primary_optimum,
                secondary_top,
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
                idle_min = secondary_optimum - primary_optimum
                n_star = len(self.artifacts.objective_participants)
                sum_lower = idle_min * max(0, n_star - 1) + secondary_optimum
                lower_clauses, secondary_top = self._cardinality_atleast(
                    self.artifacts.secondary_objective_lits,
                    sum_lower,
                    secondary_top,
                )
                bound_clauses.extend(lower_clauses)

            # No separate upper-bound encoding here. Encoding IdleSum <= B as a
            # seqcounter over the 1445 IdleSum literals costs ~263k clauses and
            # ~131k variables -- and duplicates the ITotalizer the descent below
            # already builds over the very same literals. The incumbent bound is
            # applied through that totalizer instead, at no extra cost.

        phase3_base = [
            *self.artifacts.cnf.clauses,
            *primary_clauses,
            *secondary_clauses,
            *minimum_clauses,
            *bound_clauses,
        ]

        sum_lits = self.artifacts.secondary_objective_lits
        with _new_solver(phase3_base, self.solver_name) as solver:
            if not solver.solve():
                return None, None, [
                    "phase 3 became UNSAT after fixing the phase-2 optimum"
                ], None

            model = solver.get_model()
            best_assignment, best_stats, checks = self._evaluate_sat_model(
                model,
                imposed_bound=primary_optimum,
            )
            checks.extend(
                self.model.secondary_objective_consistency_errors(model, best_stats)
            )
            if checks:
                return best_assignment, best_stats, checks, None

            best_sum = self._count_true(sum_lits, model)
            if verbose:
                print(f"[IncrementalSAT] phase-3 initial IdleSum={best_sum}")
            if best_sum == 0 or not sum_lits:
                return best_assignment, best_stats, [], best_sum

            with ITotalizer(
                lits=sum_lits,
                ubound=best_sum,
                top_id=secondary_top,
            ) as totalizer:
                solver.append_formula(totalizer.cnf.clauses)

                # Binary search on [sum_lower, best_sum]. sum_lower is the proven
                # lower bound IdleMin*(|P*|-1) + IdleMax (0 when there is no
                # IdleMax level), and best_sum is the incumbent, which is known
                # satisfiable, so lo == hi is the optimum.
                lo, hi = sum_lower, best_sum
                while lo < hi:
                    bound = (lo + hi) // 2
                    sat = solver.solve(assumptions=[-totalizer.rhs[bound]])
                    if verbose:
                        print(
                            f"[IncrementalSAT] IdleSum <= {bound}: "
                            f"{'SAT' if sat else 'UNSAT'}"
                        )
                    if not sat:
                        # Unreachable at this level, so the optimum is above it.
                        lo = bound + 1
                        continue

                    model = solver.get_model()
                    (
                        candidate_assignment,
                        candidate_stats,
                        candidate_checks,
                    ) = self._evaluate_sat_model(
                        model,
                        imposed_bound=primary_optimum,
                    )
                    candidate_checks.extend(
                        self.model.secondary_objective_consistency_errors(
                            model,
                            candidate_stats,
                            imposed_bound=bound,
                        )
                    )
                    if candidate_checks:
                        return (
                            candidate_assignment,
                            candidate_stats,
                            candidate_checks,
                            None,
                        )
                    best_assignment = candidate_assignment
                    best_stats = candidate_stats
                    hi = bound
                best_sum = lo

        final_checks = self.model.validate_assignment(best_assignment)
        if best_stats.fairness_gap != primary_optimum:
            final_checks.append(
                "lexicographic primary mismatch: "
                f"proven range={primary_optimum}, "
                f"schedule range={best_stats.fairness_gap}"
            )
        if (
            secondary_optimum is not None
            and best_stats.max_internal_idle_slots != secondary_optimum
        ):
            final_checks.append(
                "lexicographic secondary mismatch: "
                f"proven IdleMax={secondary_optimum}, "
                f"schedule IdleMax={best_stats.max_internal_idle_slots}"
            )
        if best_stats.total_internal_idle_slots != best_sum:
            final_checks.append(
                "lexicographic tertiary mismatch: "
                f"proven IdleSum={best_sum}, "
                f"schedule IdleSum={best_stats.total_internal_idle_slots}"
            )
        return best_assignment, best_stats, final_checks, best_sum

    def solve(self, verbose: bool = False) -> dict[str, Any]:
        with _new_solver(self.artifacts.cnf.clauses, self.solver_name) as solver:
            if not solver.solve():
                return self._pack_result("UNSAT", None, None)

            initial_model = solver.get_model()
            best_model = initial_model
            best_assignment, best_stats, checks = self._evaluate_sat_model(initial_model)
            if checks:
                return self._pack_result("ERROR", best_assignment, best_stats, checks)

            best_obj = best_stats.fairness_gap
            if verbose:
                print(f"[IncrementalSAT] initial IdleRange(P*)={best_obj}")

            if best_obj == 0 and self.artifacts.objective_mode == "idle-range":
                return self._pack_result(
                    "OPTIMAL",
                    best_assignment,
                    best_stats,
                    proven_optimum=0,
                )

            objective_lits = self.artifacts.objective_lits
            low = 0
            if best_obj > 0:
                if not objective_lits:
                    checks = [
                        "nonzero schedule gap but the encoder produced no objective literals"
                    ]
                    return self._pack_result("ERROR", best_assignment, best_stats, checks)

                with ITotalizer(
                    lits=objective_lits,
                    ubound=best_obj,
                    top_id=self.artifacts.n_vars,
                ) as totalizer:
                    solver.append_formula(totalizer.cnf.clauses)
                    low, high = 0, best_obj - 1

                    while low <= high:
                        bound = (low + high) // 2
                        # rhs[bound] means at least bound+1 objective literals are true.
                        # Its negation therefore imposes sum(objective_lits) <= bound.
                        sat = solver.solve(assumptions=[-totalizer.rhs[bound]])
                        if verbose:
                            print(
                                "[IncrementalSAT] IdleRange(P*) <= "
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
                                imposed_bound=bound,
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
                            best_model = candidate_model
                            high = bound - 1
                        else:
                            low = bound + 1

            final_checks = self.model.validate_assignment(best_assignment)
            if best_stats.fairness_gap != low:
                final_checks.append(
                    "optimization mismatch: "
                    f"proven optimum={low}, schedule gap={best_stats.fairness_gap}"
                )

            if (
                self.artifacts.objective_mode in LEXICOGRAPHIC_MODES
                and not final_checks
            ):
                secondary_optimum = None
                skip_max = self.artifacts.objective_mode == "lex-idlesum"
                if not skip_max:
                    (
                        best_assignment,
                        best_stats,
                        final_checks,
                        secondary_optimum,
                    ) = self._optimize_secondary(low, best_model, verbose=verbose)

                tertiary_optimum = None
                if not final_checks and (skip_max or secondary_optimum is not None):
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
    objective_mode: str = "lexicographic",
    precedence_edge_mode: str = "direct",
) -> dict[str, Any]:
    return B2BIncrementalSATSolver(
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
    objective_mode: str = "lexicographic",
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
    objective_mode: str = "lexicographic",
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
