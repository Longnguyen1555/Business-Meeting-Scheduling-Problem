from __future__ import annotations

from pathlib import Path
from typing import Any

from pysat.card import ITotalizer
from pysat.solvers import Cadical153, Glucose3

from B2B_Instance import B2BInstance, B2BSATModel, read_instance


def _ensure_instance(instance_or_path: B2BInstance | str | Path) -> B2BInstance:
    return instance_or_path if isinstance(instance_or_path, B2BInstance) else read_instance(instance_or_path)


def _new_solver(clauses: list[list[int]], preferred: str = "cadical"):
    if preferred == "glucose":
        return Glucose3(bootstrap_with=clauses)
    try:
        return Cadical153(bootstrap_with=clauses)
    except Exception:
        return Glucose3(bootstrap_with=clauses)


class B2BIncrementalSATSolver:
    """One SAT solver instance; objective bounds are imposed with assumptions."""

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        fairness_limit: int | None = 2,
        precedence_mode: str = "traditional",
        encoding_variant: str = "imp12+",
        solver_name: str = "cadical",
    ) -> None:
        self.inst = _ensure_instance(instance_or_path)
        self.model = B2BSATModel(
            inst=self.inst,
            fairness_limit=fairness_limit,
            precedence_mode=precedence_mode,
            encoding_variant=encoding_variant,
        )
        self.artifacts = self.model.build_base_cnf()
        self.solver_name = solver_name

    def _pack_result(self, status: str, assignment: list[int] | None, stats: Any | None, checks: list[str] | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "solver": "IncrementalSAT",
            "precedence_mode": self.artifacts.precedence_mode,
            "encoding_variant": self.artifacts.encoding_variant,
            "assignment": assignment,
            "stats": stats,
            "validation_errors": checks or [],
            "n_vars": self.artifacts.n_vars,
            "n_clauses": self.artifacts.n_clauses,
            "enabled_constraints": self.artifacts.enabled_constraints,
        }

    def solve(self, verbose: bool = False) -> dict[str, Any]:
        with _new_solver(self.artifacts.cnf.clauses, self.solver_name) as solver:
            if not solver.solve():
                return self._pack_result("UNSAT", None, None)

            best_assignment = self.model.decode_assignment(solver.get_model())
            best_stats = self.model.compute_stats(best_assignment)
            best_obj = best_stats.total_breaks
            if verbose:
                print(f"[IncrementalSAT] initial objective={best_obj}")

            if best_obj == 0:
                return self._pack_result("OPTIMAL", best_assignment, best_stats, self.model.validate_assignment(best_assignment))

            with ITotalizer(lits=self.artifacts.objective_lits, ubound=best_obj, top_id=self.artifacts.n_vars) as totalizer:
                solver.append_formula(totalizer.cnf.clauses)
                low, high = 0, best_obj - 1
                while low <= high:
                    bound = (low + high) // 2
                    # not(rhs[bound]) means fewer than bound+1 true literals, i.e. <= bound.
                    sat = solver.solve(assumptions=[-totalizer.rhs[bound]])
                    if verbose:
                        print(f"[IncrementalSAT] objective <= {bound}: {'SAT' if sat else 'UNSAT'}")
                    if sat:
                        best_assignment = self.model.decode_assignment(solver.get_model())
                        best_stats = self.model.compute_stats(best_assignment)
                        high = bound - 1
                    else:
                        low = bound + 1

            return self._pack_result("OPTIMAL", best_assignment, best_stats, self.model.validate_assignment(best_assignment))


def solve_b2b(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = 2,
    precedence_mode: str = "traditional",
    encoding_variant: str = "imp12+",
    verbose: bool = False,
) -> dict[str, Any]:
    return B2BIncrementalSATSolver(
        instance_or_path=instance_or_path,
        fairness_limit=fairness_limit,
        precedence_mode=precedence_mode,
        encoding_variant=encoding_variant,
    ).solve(verbose=verbose)


def solve_b2b_traditional(instance_or_path: B2BInstance | str | Path, fairness_limit: int | None = 2, encoding_variant: str = "imp12+", verbose: bool = False) -> dict[str, Any]:
    return solve_b2b(instance_or_path, fairness_limit, "traditional", encoding_variant, verbose)


def solve_b2b_staircase(instance_or_path: B2BInstance | str | Path, fairness_limit: int | None = 2, encoding_variant: str = "imp12+", verbose: bool = False) -> dict[str, Any]:
    return solve_b2b(instance_or_path, fairness_limit, "staircase", encoding_variant, verbose)
