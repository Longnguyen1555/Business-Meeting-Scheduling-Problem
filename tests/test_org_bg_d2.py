from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import B2BInstance
from MaxSAT_Solver import B2BMaxSATSolver
from ORG_BG_D2 import solve_instance


def _fixed_instance(*, violate_cap: bool) -> B2BInstance:
    slots = [0, 2, 4, 6] if violate_cap else [0, 1, 4, 5]
    return B2BInstance(
        n_business=5,
        n_meetings=4,
        n_tables=1,
        n_total_slots=7,
        n_morning_slots=3,
        requested=[(0, 1, 3), (0, 2, 3), (0, 3, 3), (0, 4, 3)],
        meetings_by_business=[[0, 1, 2, 3], [0], [1], [2], [3]],
        n_meetings_business=[4, 1, 1, 1, 1],
        forbidden=[set() for _ in range(5)],
        fixed=slots,
        precedences=[set() for _ in range(4)],
        instance_name="org-bg-fixed",
    )


class HistoricalBGBaselineTests(unittest.TestCase):
    def test_independent_org_and_compact_agree_on_fixed_optimum(self) -> None:
        instance = _fixed_instance(violate_cap=False)
        historical = solve_instance(
            instance,
            backend="rc2",
            timeout=30,
            uwrmaxsat_binary=None,
            uwrmaxsat_sha256="",
        )
        compact = B2BMaxSATSolver(
            instance,
            precedence_encoding="sparse_suffix",
            precedence_graph="distance_closure",
            domain_filter_graph="distance_closure",
            encoding_variant="imp12+",
            domain_mode="reduced",
            objective_mode="bg_d2",
            backend="rc2",
        ).solve()
        self.assertEqual(historical["status"], "OPTIMAL")
        self.assertEqual(compact["status"], "OPTIMAL")
        self.assertEqual(historical["objective_vector"], "1")
        self.assertEqual(tuple(compact["objective_vector"]), (1,))
        self.assertEqual(historical["validation_errors"], "")
        self.assertLessEqual(historical["break_group_range"], 2)
        self.assertEqual(
            historical["model_configuration_display_name"],
            "Published-style/Full",
        )
        self.assertEqual(
            historical["implementation_provenance"],
            "independent_reimplementation",
        )

    def test_both_models_reject_base_feasible_fairness_cap_violation(self) -> None:
        instance = _fixed_instance(violate_cap=True)
        historical = solve_instance(
            instance,
            backend="rc2",
            timeout=30,
            uwrmaxsat_binary=None,
            uwrmaxsat_sha256="",
        )
        compact = B2BMaxSATSolver(
            instance,
            precedence_mode="traditional",
            encoding_variant="basic",
            objective_mode="bg_d2",
            backend="rc2",
        ).solve()
        self.assertEqual(historical["status"], "UNSAT")
        self.assertEqual(compact["status"], "UNSAT")


if __name__ == "__main__":
    unittest.main()
