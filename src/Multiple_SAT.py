from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from pysat.card import CardEnc, EncType

from B2B_Instance import B2BInstance, B2BSATModel, B2BSolutionStats, read_instance
from Journal_Metrics import objective_metric_errors
from SAT_Backend import (
    create_sat_solver,
    normalize_sat_backend,
    sat_backend_label,
    sat_backend_version,
)


def _ensure_instance(instance_or_path: B2BInstance | str | Path) -> B2BInstance:
    return (
        instance_or_path
        if isinstance(instance_or_path, B2BInstance)
        else read_instance(instance_or_path)
    )


def _new_solver(clauses: list[list[int]], preferred: str = "cadical"):
    """Create exactly the requested SAT backend without fallback."""

    return create_sat_solver(clauses, preferred)


class B2BMultipleSATSolver:
    """Fresh-solver sequential optimization of exact objective tiers.

    Every candidate bound is solved in a fresh SAT solver. After proving one
    tier optimal, an exact upper bound is added before the next tier.
    """

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        precedence_mode: str | None = None,
        encoding_variant: str = "imp12+",
        solver_name: str = "cadical",
        domain_mode: str = "reduced",
        *,
        precedence_encoding: str | None = None,
        precedence_graph: str | None = None,
        domain_filter_graph: str = "distance_closure",
        objective_mode: str = "ir",
    ) -> None:
        if (
            precedence_mode is None
            and precedence_encoding is None
            and precedence_graph is None
        ):
            precedence_mode = "traditional"
        self.inst = _ensure_instance(instance_or_path)
        self.model = B2BSATModel(
            inst=self.inst,
            precedence_mode=precedence_mode,
            precedence_encoding=precedence_encoding,
            precedence_graph=precedence_graph,
            encoding_variant=encoding_variant,
            domain_mode=domain_mode,
            domain_filter_graph=domain_filter_graph,
            objective_mode=objective_mode,
        )
        self.artifacts = self.model.build_base_cnf()
        self.solver_name = normalize_sat_backend(solver_name)
        self.solver_backend = sat_backend_label(self.solver_name)
        self.solver_version = sat_backend_version(self.solver_name)
        self.n_optimizer_calls = 0
        self.n_bound_encodings = 0
        self.optimizer_added_variables_peak = 0
        self.optimizer_added_clauses_peak = 0
        self.optimizer_added_literals_peak = 0
        self.optimizer_added_clauses_cumulative = 0
        self.objective_phase_seconds: list[float] = []
        self.objective_phase_calls: list[int] = []

    def _pack_result(
        self,
        status: str,
        assignment: list[int] | None,
        stats: B2BSolutionStats | None,
        checks: list[str] | None = None,
        *,
        proven_optimum: int | None = None,
        proven_objective_vector: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "solver": "MultipleSAT",
            "solver_backend": self.solver_backend,
            "solver_version": self.solver_version,
            "sat_backend_preference": self.solver_name,
            "precedence_mode": self.artifacts.precedence_mode,
            "precedence_encoding": self.artifacts.precedence_encoding,
            "precedence_graph": self.artifacts.precedence_graph,
            "precedence_configuration": (
                self.artifacts.precedence_configuration
            ),
            "encoding_variant": self.artifacts.encoding_variant,
            "domain_mode": self.artifacts.domain_mode,
            "domain_filter_graph": self.artifacts.domain_filter_graph,
            "objective": self.artifacts.objective_name,
            "objective_mode": self.artifacts.objective_mode,
            "objective_participant_count": len(
                self.artifacts.objective_participants
            ),
            "objective_participants": tuple(
                participant + 1
                for participant in self.artifacts.objective_participants
            ),
            "objective_vector": (
                stats.objective_vector
                if stats is not None
                else proven_objective_vector
            ),
            "objective_value": (
                stats.objective_vector[0]
                if stats is not None
                else proven_optimum
            ),
            "primary_objective_value": (
                stats.objective_vector[0] if stats is not None else proven_optimum
            ),
            "secondary_objective_value": (
                stats.objective_vector[1]
                if stats is not None and len(stats.objective_vector) > 1
                else None
            ),
            "tertiary_objective_value": (
                stats.objective_vector[2]
                if stats is not None and len(stats.objective_vector) > 2
                else None
            ),
            "proven_optimum": proven_optimum,
            "proven_objective_vector": proven_objective_vector,
            "objective_phase_seconds": tuple(self.objective_phase_seconds),
            "objective_phase_calls": tuple(self.objective_phase_calls),
            "assignment": assignment,
            "stats": stats,
            "validation_errors": checks or [],
            "n_vars": self.artifacts.n_vars,
            "n_clauses": self.artifacts.n_clauses,
            "n_hard_clauses": self.artifacts.n_clauses,
            "n_soft": 0,
            "n_soft_clauses": 0,
            "n_objective_lits": sum(
                len(tier.literals) for tier in self.artifacts.objective_tiers
            ),
            "full_schedule_candidates": (
                self.artifacts.full_schedule_candidates
            ),
            "unary_eligible_schedule_candidates": (
                self.artifacts.unary_eligible_schedule_candidates
            ),
            "initial_schedule_candidates": (
                self.artifacts.initial_schedule_candidates
            ),
            "reduced_schedule_candidates": (
                self.artifacts.reduced_schedule_candidates
            ),
            "active_schedule_candidates": (
                self.artifacts.active_schedule_candidates
            ),
            "unary_removed_schedule_candidates": (
                self.artifacts.unary_removed_schedule_candidates
            ),
            "preprocessing_removed_schedule_candidates": (
                self.artifacts.preprocessing_removed_schedule_candidates
            ),
            "removed_schedule_candidates": (
                self.artifacts.removed_schedule_candidates
            ),
            "precedence_direct_edges": self.artifacts.precedence_direct_edges,
            "precedence_closure_edges": (
                self.artifacts.precedence_transitive_edges
            ),
            "precedence_max_distance": self.artifacts.precedence_max_distance,
            "precedence_relation_edges": (
                self.artifacts.precedence_relation_edges
            ),
            "precedence_pairwise_clauses": (
                self.artifacts.precedence_pairwise_clauses
            ),
            "precedence_sparse_link_clauses": (
                self.artifacts.precedence_sparse_link_clauses
            ),
            "precedence_unique_suffix_cuts": (
                self.artifacts.precedence_unique_suffix_cuts
            ),
            "enabled_constraints": self.artifacts.enabled_constraints,
            "n_optimizer_calls": self.n_optimizer_calls,
            "n_bound_encodings": self.n_bound_encodings,
            "optimizer_added_variables_peak": (
                self.optimizer_added_variables_peak
            ),
            "optimizer_added_clauses_peak": self.optimizer_added_clauses_peak,
            "optimizer_added_literals_peak": self.optimizer_added_literals_peak,
            "optimizer_added_clauses_cumulative": (
                self.optimizer_added_clauses_cumulative
            ),
        }

    def _evaluate_sat_model(
        self,
        sat_model: list[int],
        *,
        imposed_bound: int | None = None,
        imposed_bounds: tuple[int, ...] | None = None,
    ) -> tuple[list[int], B2BSolutionStats, list[str]]:
        assignment = self.model.decode_assignment(sat_model)
        stats = self.model.compute_stats(assignment)
        checks = self.model.validate_assignment(assignment)
        checks.extend(
            self.model.objective_consistency_errors(
                sat_model,
                stats,
                imposed_bound=imposed_bound,
                imposed_bounds=imposed_bounds,
            )
        )
        checks.extend(
            objective_metric_errors(
                self.inst,
                assignment,
                objective_mode=self.artifacts.objective_mode,
                encoded_vector=self.model.encoded_objective_vector(sat_model),
            )
        )
        return assignment, stats, checks

    def _bound_clauses(
        self,
        lits: tuple[int, ...],
        bound: int,
        *,
        top_id: int,
    ) -> tuple[list[list[int]], int]:
        if bound < 0:
            clauses = [[]]
            encoded_top_id = top_id
        elif not lits or bound >= len(lits):
            clauses = []
            encoded_top_id = top_id
        elif bound == 0:
            clauses = [[-lit] for lit in lits]
            encoded_top_id = top_id
        else:
            encoding = CardEnc.atmost(
                lits=list(lits),
                bound=bound,
                top_id=top_id,
                encoding=EncType.seqcounter,
            )
            clauses = encoding.clauses
            encoded_top_id = encoding.nv

        self.n_bound_encodings += 1
        self.optimizer_added_variables_peak = max(
            self.optimizer_added_variables_peak,
            max(0, encoded_top_id - self.artifacts.n_vars),
        )
        self.optimizer_added_clauses_peak = max(
            self.optimizer_added_clauses_peak,
            len(clauses),
        )
        self.optimizer_added_literals_peak = max(
            self.optimizer_added_literals_peak,
            sum(len(clause) for clause in clauses),
        )
        self.optimizer_added_clauses_cumulative += len(clauses)
        return clauses, encoded_top_id

    def solve(
        self,
        verbose: bool = False,
        incumbent_callback: Callable[[int | tuple[int, ...]], None] | None = None,
    ) -> dict[str, Any]:
        base_clauses = self.artifacts.cnf.clauses
        with _new_solver(base_clauses, self.solver_name) as solver:
            self.n_optimizer_calls += 1
            if not solver.solve():
                return self._pack_result("UNSAT", None, None)
            initial_model = solver.get_model()

        best_assignment, best_stats, checks = self._evaluate_sat_model(initial_model)
        if checks:
            return self._pack_result("ERROR", best_assignment, best_stats, checks)

        best_vector = best_stats.objective_vector
        if incumbent_callback is not None:
            incumbent_callback(best_vector[0] if len(best_vector) == 1 else best_vector)
        if verbose:
            print(f"[MultipleSAT] initial objective vector={best_vector}")

        fixed_clauses: list[list[int]] = []
        fixed_top_id = self.artifacts.n_vars
        proven: list[int] = []
        for phase_index, tier in enumerate(self.artifacts.objective_tiers):
            phase_started = time.perf_counter()
            calls_before = self.n_optimizer_calls
            current = best_stats.objective_vector[phase_index]
            low, high = 0, current - 1
            while low <= high:
                bound = (low + high) // 2
                bound_clauses, _ = self._bound_clauses(
                    tier.literals,
                    bound,
                    top_id=fixed_top_id,
                )
                with _new_solver(
                    [*base_clauses, *fixed_clauses, *bound_clauses],
                    self.solver_name,
                ) as solver:
                    self.n_optimizer_calls += 1
                    satisfiable = solver.solve()
                    if verbose:
                        print(
                            f"[MultipleSAT] {tier.name} <= {bound}: "
                            f"{'SAT' if satisfiable else 'UNSAT'}"
                        )
                    if satisfiable:
                        candidate_model = solver.get_model()
                        candidate_assignment, candidate_stats, candidate_checks = (
                            self._evaluate_sat_model(
                                candidate_model,
                                imposed_bounds=tuple([*proven, bound]),
                            )
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
                        if incumbent_callback is not None:
                            vector = best_stats.objective_vector
                            incumbent_callback(
                                vector[0] if len(vector) == 1 else vector
                            )
                        high = bound - 1
                    else:
                        low = bound + 1

            optimum = low
            proven.append(optimum)
            phase_fix, fixed_top_id = self._bound_clauses(
                tier.literals,
                optimum,
                top_id=fixed_top_id,
            )
            fixed_clauses.extend(phase_fix)
            self.objective_phase_seconds.append(
                time.perf_counter() - phase_started
            )
            self.objective_phase_calls.append(
                self.n_optimizer_calls - calls_before
            )

        final_checks = self.model.validate_assignment(best_assignment)
        final_checks.extend(
            objective_metric_errors(
                self.inst,
                best_assignment,
                objective_mode=self.artifacts.objective_mode,
                encoded_vector=best_stats.objective_vector,
            )
        )
        if best_stats.objective_vector != tuple(proven):
            final_checks.append(
                "optimization mismatch: "
                f"proven vector={tuple(proven)}, "
                f"schedule vector={best_stats.objective_vector}"
            )
        return self._pack_result(
            "OPTIMAL" if not final_checks else "ERROR",
            best_assignment,
            best_stats,
            final_checks,
            proven_optimum=proven[0],
            proven_objective_vector=tuple(proven),
        )


def solve_b2b(
    instance_or_path: B2BInstance | str | Path,
    precedence_mode: str | None = None,
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    solver_name: str = "cadical",
    domain_mode: str = "reduced",
    *,
    precedence_encoding: str | None = None,
    precedence_graph: str | None = None,
    domain_filter_graph: str = "distance_closure",
    objective_mode: str = "ir",
) -> dict[str, Any]:
    return B2BMultipleSATSolver(
        instance_or_path=instance_or_path,
        precedence_mode=precedence_mode,
        precedence_encoding=precedence_encoding,
        precedence_graph=precedence_graph,
        encoding_variant=encoding_variant,
        solver_name=solver_name,
        domain_mode=domain_mode,
        domain_filter_graph=domain_filter_graph,
        objective_mode=objective_mode,
    ).solve(verbose=verbose)


def solve_b2b_traditional(
    instance_or_path: B2BInstance | str | Path,
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    domain_mode: str = "reduced",
    domain_filter_graph: str = "distance_closure",
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path=instance_or_path,
        precedence_mode="traditional",
        encoding_variant=encoding_variant,
        verbose=verbose,
        domain_mode=domain_mode,
        domain_filter_graph=domain_filter_graph,
    )


def solve_b2b_staircase(
    instance_or_path: B2BInstance | str | Path,
    encoding_variant: str = "imp12+",
    verbose: bool = False,
    domain_mode: str = "reduced",
    domain_filter_graph: str = "distance_closure",
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path=instance_or_path,
        precedence_mode="staircase",
        encoding_variant=encoding_variant,
        verbose=verbose,
        domain_mode=domain_mode,
        domain_filter_graph=domain_filter_graph,
    )
