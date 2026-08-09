from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pysat.examples.rc2 import RC2
from pysat.formula import WCNF

from B2B_Instance import B2BInstance, B2BSATModel, read_instance


def _ensure_instance(
    instance_or_path: B2BInstance | str | Path,
) -> B2BInstance:
    return (
        instance_or_path
        if isinstance(instance_or_path, B2BInstance)
        else read_instance(instance_or_path)
    )


class LexicographicIdleRC2Oracle:
    """Exact correctness oracle for ``min_lex (IdleRange(P*), IdleSum)``.

    This development-only oracle deliberately reuses the conference model
    without changing its default objective or benchmark schema. Let ``U`` be
    the number of exact participant-idle threshold literals. The weighted
    MaxSAT cost is

        (U + 1) * IdleRange(P*) + IdleSum.

    Since ``0 <= IdleSum <= U``, minimizing this scalar is exactly equivalent
    to lexicographically minimizing ``(IdleRange(P*), IdleSum)``. Production
    integration can later use either these dominating weights or two exact
    optimization phases, but it must agree with this oracle on the complete
    objective vector.
    """

    def __init__(
        self,
        instance_or_path: B2BInstance | str | Path,
        precedence_mode: str | None = None,
        encoding_variant: str = "imp12+",
        domain_mode: str = "reduced",
        *,
        precedence_encoding: str | None = None,
        precedence_graph: str | None = None,
        domain_filter_graph: str = "distance_closure",
    ) -> None:
        if (
            precedence_mode is None
            and precedence_encoding is None
            and precedence_graph is None
        ):
            precedence_mode = "traditional"
        self.inst = _ensure_instance(instance_or_path)
        self.model = B2BSATModel(
            self.inst,
            precedence_mode=precedence_mode,
            precedence_encoding=precedence_encoding,
            precedence_graph=precedence_graph,
            encoding_variant=encoding_variant,
            domain_mode=domain_mode,
            domain_filter_graph=domain_filter_graph,
        )
        self.artifacts = self.model.build_base_cnf()
        self.idle_sum_lits = [
            literal
            for participant in self.artifacts.objective_participants
            for literal in self.artifacts.sorted_hole_lits_by_participant[
                participant
            ]
        ]
        self.idle_sum_upper_bound = len(self.idle_sum_lits)
        self.primary_weight = self.idle_sum_upper_bound + 1

    def build_wcnf(self) -> WCNF:
        wcnf = WCNF()
        for clause in self.artifacts.cnf.clauses:
            wcnf.append(clause)
        for literal in self.artifacts.objective_lits:
            wcnf.append([-literal], weight=self.primary_weight)
        for literal in self.idle_sum_lits:
            wcnf.append([-literal], weight=1)
        return wcnf

    @staticmethod
    def _true_count(model: list[int], literals: list[int]) -> int:
        positives = {literal for literal in model if literal > 0}
        return sum(literal in positives for literal in literals)

    def solve(self) -> dict[str, Any]:
        with RC2(self.build_wcnf()) as solver:
            sat_model = solver.compute()
            if sat_model is None:
                return {
                    "status": "UNSAT",
                    "objective_mode": "idle_range_then_idle_sum",
                    "objective_vector": None,
                    "assignment": None,
                    "validation_errors": [],
                }
            scalar_cost = int(solver.cost)

        assignment = self.model.decode_assignment(sat_model)
        stats = self.model.compute_stats(assignment)
        encoded_idle_range = self._true_count(
            sat_model,
            self.artifacts.objective_lits,
        )
        encoded_idle_sum = self._true_count(
            sat_model,
            self.idle_sum_lits,
        )
        recomputed_scalar_cost = (
            self.primary_weight * stats.idle_range
            + stats.total_internal_idle_slots
        )

        checks = self.model.validate_assignment(assignment)
        checks.extend(
            self.model.objective_consistency_errors(sat_model, stats)
        )
        if encoded_idle_range != stats.idle_range:
            checks.append(
                "lexicographic primary mismatch: "
                f"encoded={encoded_idle_range}, schedule={stats.idle_range}"
            )
        if encoded_idle_sum != stats.total_internal_idle_slots:
            checks.append(
                "lexicographic secondary mismatch: "
                f"encoded={encoded_idle_sum}, "
                f"schedule={stats.total_internal_idle_slots}"
            )
        if scalar_cost != recomputed_scalar_cost:
            checks.append(
                "lexicographic scalar-cost mismatch: "
                f"solver={scalar_cost}, recomputed={recomputed_scalar_cost}"
            )

        return {
            "status": "OPTIMAL" if not checks else "ERROR",
            "solver": "RC2CorrectnessOracle",
            "objective": (
                "lexicographic_internal_idle_range_pstar_then_idle_sum"
            ),
            "objective_mode": "idle_range_then_idle_sum",
            "objective_vector": (
                stats.idle_range,
                stats.total_internal_idle_slots,
            ),
            "primary_objective_value": stats.idle_range,
            "secondary_objective_value": stats.total_internal_idle_slots,
            "lexicographic_scalar_cost": scalar_cost,
            "lexicographic_primary_weight": self.primary_weight,
            "idle_sum_upper_bound": self.idle_sum_upper_bound,
            "assignment": assignment,
            "stats": stats,
            "validation_errors": checks,
            "n_hard_clauses": len(self.artifacts.cnf.clauses),
            "n_primary_objective_lits": len(
                self.artifacts.objective_lits
            ),
            "n_secondary_objective_lits": len(self.idle_sum_lits),
        }


def _json_ready(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key != "stats"
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Development correctness oracle for lexicographic "
            "min(IdleRange(P*), IdleSum)."
        )
    )
    parser.add_argument("instance", help="B2B .dzn instance")
    parser.add_argument(
        "--domain-mode",
        choices=("full", "reduced"),
        default="reduced",
    )
    parser.add_argument(
        "--precedence-encoding",
        choices=("pairwise", "sparse_suffix"),
        default="pairwise",
    )
    parser.add_argument(
        "--precedence-graph",
        choices=("direct", "distance_closure"),
        default="direct",
    )
    parser.add_argument(
        "--domain-filter-graph",
        choices=("direct", "distance_closure"),
        default="distance_closure",
    )
    args = parser.parse_args(argv)

    result = LexicographicIdleRC2Oracle(
        args.instance,
        domain_mode=args.domain_mode,
        precedence_encoding=args.precedence_encoding,
        precedence_graph=args.precedence_graph,
        domain_filter_graph=args.domain_filter_graph,
    ).solve()
    print(json.dumps(_json_ready(result), indent=2))
    return 0 if result["status"] in {"OPTIMAL", "UNSAT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
