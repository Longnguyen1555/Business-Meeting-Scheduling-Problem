from __future__ import annotations

from pathlib import Path
from typing import Any

from pysat.examples.rc2 import RC2
from pysat.formula import WCNF

from B2B_Instance import (
    LEXICOGRAPHIC_MODES,
    B2BInstance,
    B2BSATModel,
    B2BSolutionStats,
    read_instance,
)


#: Weight of one IdleMax unit in the single-phase lexicographic WCNF. Chosen to
#: dwarf any attainable IdleSum so IdleMax always outranks the sum.
SECONDARY_OBJECTIVE_WEIGHT = 10000

#: Weight of one IdleRange unit. Dwarfs any attainable (IdleMax, IdleSum) pair,
#: so the range dominates both lower levels.
PRIMARY_OBJECTIVE_WEIGHT = SECONDARY_OBJECTIVE_WEIGHT * 10000


def _ensure_instance(instance_or_path: B2BInstance | str | Path) -> B2BInstance:
    return (
        instance_or_path
        if isinstance(instance_or_path, B2BInstance)
        else read_instance(instance_or_path)
    )


class B2BMaxSATSolver:
    """MaxSAT optimization of the internal-idle-slot range over P*.

    B2B_Instance exposes unary objective literals whose true count is exactly
    max_{p in P*} B(p) - min_{p in P*} B(p), where P* contains participants
    with at least two meetings. RC2 minimizes that count using unit soft clauses
    [-lit]. In ``lexicographic`` mode a single RC2 run minimizes both objectives
    at once, weighting the range literals above any achievable IdleMax so that
    the range dominates and the bottleneck max_p B(p) breaks ties. An optional
    fairness_limit adds a hard upper bound on the same range.
    """

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        fairness_limit: int | None = None,
        precedence_mode: str = "traditional",
        encoding_variant: str = "imp12+",
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

    def _build_wcnf(self) -> WCNF:
        """Use the exact hard and soft clauses produced by the shared encoder."""
        return self.model.build_wcnf()

    def _build_lexicographic_wcnf(self) -> tuple[WCNF, int, int]:
        """Encode all three objectives in one WCNF using separating weights.

        RC2 minimizes

            W1 * IdleRange + W2 * IdleMax + IdleSum

        with W1 = PRIMARY_OBJECTIVE_WEIGHT and W2 = SECONDARY_OBJECTIVE_WEIGHT.
        IdleSum cannot reach W2 and (W2 * IdleMax + IdleSum) cannot reach W1, so
        no saving at a lower level can ever pay for one unit at a higher level.
        The optimum is therefore the lexicographic optimum, and two divmods
        recover the three components.
        """
        wcnf = WCNF()
        for clause in self.artifacts.cnf.clauses:
            wcnf.append(clause)

        # lex-idlesum drops the IdleMax level entirely: no soft clauses for it,
        # and w2 collapses to 1 so divmod yields (IdleRange, 0, IdleSum).
        skip_max = self.artifacts.objective_mode == "lex-idlesum"
        max_lits = [] if skip_max else self.model.max_break_lits()
        sum_lits = self.artifacts.secondary_objective_lits
        w1 = PRIMARY_OBJECTIVE_WEIGHT
        w2 = 1 if skip_max else SECONDARY_OBJECTIVE_WEIGHT

        # Guard the separation rather than let a silent carry corrupt the order.
        if not skip_max and len(sum_lits) >= w2:
            raise ValueError(
                f"IdleSum upper bound {len(sum_lits)} does not fit under the "
                f"secondary weight {w2}; lexicographic order would break"
            )
        if w2 * len(max_lits) + len(sum_lits) >= w1:
            raise ValueError(
                f"(IdleMax, IdleSum) upper bound does not fit under the primary "
                f"weight {w1}; lexicographic order would break"
            )

        for lit in self.artifacts.objective_lits:
            wcnf.append([-lit], weight=w1)
        for lit in max_lits:
            wcnf.append([-lit], weight=w2)
        for lit in sum_lits:
            wcnf.append([-lit], weight=1)
        return wcnf, w1, w2

    def _pack_result(
        self,
        status: str,
        assignment: list[int] | None,
        stats: B2BSolutionStats | None,
        checks: list[str] | None = None,
        *,
        solver_cost: int | None = None,
        secondary_optimum: int | None = None,
        tertiary_optimum: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "solver": "MaxSAT",
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
                stats.fairness_gap if stats is not None else solver_cost
            ),
            "proven_optimum": solver_cost,
            "solver_cost": solver_cost,
            "secondary_objective_value": (
                stats.max_internal_idle_slots
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
            "n_soft": (
                len(self.artifacts.objective_lits)
                + (
                    len(self.model.max_break_lits())
                    if self.artifacts.objective_mode == "lexicographic"
                    else 0
                )
                + (
                    len(self.artifacts.secondary_objective_lits)
                    if self.artifacts.objective_mode in LEXICOGRAPHIC_MODES
                    else 0
                )
            ),
            "n_primary_objective_lits": len(self.artifacts.objective_lits),
            "n_secondary_objective_lits": len(self.model.max_break_lits()),
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

    def _solve_lexicographic(self, verbose: bool = False) -> dict[str, Any]:
        """Optimize all three objectives in a single weighted RC2 call."""
        wcnf, w1, w2 = self._build_lexicographic_wcnf()

        with RC2(wcnf) as solver:
            sat_model = solver.compute()
            if sat_model is None:
                return self._pack_result("UNSAT", None, None)
            primary_optimum, remainder = divmod(int(solver.cost), w1)
            secondary_optimum, tertiary_optimum = divmod(remainder, w2)
            if w2 == 1:
                # No IdleMax level: the whole remainder is IdleSum.
                secondary_optimum, tertiary_optimum = None, remainder

        assignment = self.model.decode_assignment(sat_model)
        stats = self.model.compute_stats(assignment)
        checks = self.model.validate_assignment(assignment)
        checks.extend(
            self.model.objective_consistency_errors(
                sat_model,
                stats,
                solver_cost=primary_optimum,
            )
        )
        if secondary_optimum is not None:
            checks.extend(
                self.model.secondary_max_consistency_errors(
                    sat_model,
                    stats,
                    solver_cost=secondary_optimum,
                )
            )
        checks.extend(
            self.model.secondary_objective_consistency_errors(
                sat_model,
                stats,
                solver_cost=tertiary_optimum,
            )
        )

        if verbose:
            print(
                "[MaxSAT] lexicographic optimum="
                f"({stats.fairness_gap}, {stats.max_internal_idle_slots}, "
                f"{stats.total_internal_idle_slots})"
            )
        return self._pack_result(
            "OPTIMAL" if not checks else "ERROR",
            assignment,
            stats,
            checks,
            solver_cost=primary_optimum,
            secondary_optimum=secondary_optimum,
            tertiary_optimum=tertiary_optimum,
        )

    def solve(self, verbose: bool = False) -> dict[str, Any]:
        if self.artifacts.objective_mode in LEXICOGRAPHIC_MODES:
            return self._solve_lexicographic(verbose=verbose)

        wcnf = self._build_wcnf()

        with RC2(wcnf) as solver:
            primary_model = solver.compute()
            if primary_model is None:
                return self._pack_result("UNSAT", None, None)

            assignment = self.model.decode_assignment(primary_model)
            stats = self.model.compute_stats(assignment)
            primary_optimum = int(solver.cost)

            checks = self.model.validate_assignment(assignment)
            checks.extend(
                self.model.objective_consistency_errors(
                    primary_model,
                    stats,
                    solver_cost=primary_optimum,
                )
            )

        if checks:
            return self._pack_result(
                "ERROR",
                assignment,
                stats,
                checks,
                solver_cost=primary_optimum,
            )

        if verbose:
            print(
                "[MaxSAT] optimum IdleRange(P*)="
                f"{stats.fairness_gap} (RC2 cost={primary_optimum})"
            )

        return self._pack_result(
            "OPTIMAL",
            assignment,
            stats,
            checks,
            solver_cost=primary_optimum,
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
    return B2BMaxSATSolver(
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
