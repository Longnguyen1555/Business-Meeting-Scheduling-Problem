from __future__ import annotations

from pathlib import Path
from typing import Any

from pysat.card import CardEnc, EncType
from pysat.solvers import Cadical153

from B2B_Instance import B2BInstance, B2BSATModel, read_instance


def _ensure_instance(instance_or_path: B2BInstance | str | Path) -> B2BInstance:
    if isinstance(instance_or_path, B2BInstance):
        return instance_or_path
    return read_instance(instance_or_path)


class B2BMultipleSATSolver:
    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        fairness_limit: int | None = None,
        precedence_mode: str = "staircase",
    ) -> None:
        self.inst = _ensure_instance(instance_or_path)
        self.model = B2BSATModel(
            inst=self.inst,
            fairness_limit=fairness_limit,
            precedence_mode=precedence_mode,
        )
        self.artifacts = self.model.build_base_cnf()
        self.base_top_id = self.model.vpool.top

    def solve(self, verbose: bool = False) -> dict[str, Any]:
        with Cadical153(bootstrap_with=self.artifacts.cnf.clauses) as solver:
            if not solver.solve():
                return {
                    "status": "UNSAT",
                    "solver": "MultipleSAT",
                    "assignment": None,
                    "stats": None,
                    "n_vars": self.artifacts.n_vars,
                    "n_clauses": self.artifacts.n_clauses,
                }
            model = solver.get_model()

        best_assignment = self.model.decode_assignment(model)
        best_stats = self.model.compute_stats(best_assignment)

        if verbose:
            print(f"[MultipleSAT] initial feasible total_breaks = {best_stats.total_breaks}")

        if best_stats.total_breaks == 0:
            return {
                "status": "OPTIMAL",
                "solver": "MultipleSAT",
                "assignment": best_assignment,
                "stats": best_stats,
                "n_vars": self.artifacts.n_vars,
                "n_clauses": self.artifacts.n_clauses,
            }

        low = 0
        high = best_stats.total_breaks - 1

        while low <= high:
            bound = (low + high) // 2
            bound_cnf = CardEnc.atmost(
                lits=self.artifacts.objective_lits,
                bound=bound,
                top_id=self.base_top_id,
                encoding=EncType.seqcounter,
            )

            with Cadical153(bootstrap_with=self.artifacts.cnf.clauses) as solver:
                solver.append_formula(bound_cnf.clauses)
                sat = solver.solve()

                if verbose:
                    print(f"[MultipleSAT] try total_breaks <= {bound} -> {'SAT' if sat else 'UNSAT'}")

                if sat:
                    model = solver.get_model()
                    best_assignment = self.model.decode_assignment(model)
                    best_stats = self.model.compute_stats(best_assignment)
                    high = bound - 1
                else:
                    low = bound + 1

        return {
            "status": "OPTIMAL",
            "solver": "MultipleSAT",
            "assignment": best_assignment,
            "stats": best_stats,
            "n_vars": self.artifacts.n_vars,
            "n_clauses": self.artifacts.n_clauses,
        }


def solve_b2b(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    precedence_mode: str = "staircase",
    verbose: bool = False,
) -> dict[str, Any]:
    solver = B2BMultipleSATSolver(
        instance_or_path=instance_or_path,
        fairness_limit=fairness_limit,
        precedence_mode=precedence_mode,
    )
    result = solver.solve(verbose=verbose)
    result["precedence_mode"] = precedence_mode
    return result


def solve_b2b_traditional(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path=instance_or_path,
        fairness_limit=fairness_limit,
        precedence_mode="traditional",
        verbose=verbose,
    )


def solve_b2b_staircase(
    instance_or_path: B2BInstance | str | Path,
    fairness_limit: int | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    return solve_b2b(
        instance_or_path=instance_or_path,
        fairness_limit=fairness_limit,
        precedence_mode="staircase",
        verbose=verbose,
    )


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "forum-13.original.dzn"
    result = solve_b2b(target, fairness_limit=2, precedence_mode="staircase", verbose=True)
    print(result["status"])
    if result["stats"] is not None:
        print("total_breaks =", result["stats"].total_breaks)
        print("fairness_gap =", result["stats"].fairness_gap)
