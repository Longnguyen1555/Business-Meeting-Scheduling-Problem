from __future__ import annotations

import sys
import unittest
from itertools import product
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import B2BInstance, B2BSATModel
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from MaxSAT_Solver import B2BMaxSATSolver
from Multiple_SAT import B2BMultipleSATSolver


def _instance_with_single_meeting_participant() -> B2BInstance:
    """Return a fixed schedule where range(P*) differs from range(P).

    Fixed meeting slots are [0, 4, 2, 1]. The internal-idle totals are:
      participant 1: 2, participant 2: 1, participant 3: 1,
      participant 4: 0.
    Participant 4 has one meeting and is excluded from P*. Hence
    IdleRange(P*)=1 while IdleRange(P)=2.
    """

    requested = [
        (0, 1, 3),
        (0, 2, 3),
        (1, 2, 3),
        (3, 0, 3),
    ]
    meetings_by_business = [
        [0, 1, 3],
        [0, 2],
        [1, 2],
        [3],
    ]
    return B2BInstance(
        n_business=4,
        n_meetings=4,
        n_tables=2,
        n_total_slots=5,
        n_morning_slots=2,
        requested=requested,
        meetings_by_business=meetings_by_business,
        n_meetings_business=[3, 2, 2, 1],
        forbidden=[set() for _ in range(4)],
        fixed=[0, 4, 2, 1],
        precedences=[set() for _ in range(4)],
        instance_name="pstar-differs-from-all",
    )


def _instance_with_one_objective_participant() -> B2BInstance:
    """Return an instance whose P* contains exactly one participant."""

    return B2BInstance(
        n_business=3,
        n_meetings=2,
        n_tables=1,
        n_total_slots=5,
        n_morning_slots=2,
        requested=[(0, 1, 3), (0, 2, 3)],
        meetings_by_business=[[0, 1], [0], [1]],
        n_meetings_business=[2, 1, 1],
        forbidden=[set() for _ in range(3)],
        fixed=[0, 4],
        precedences=[set(), set()],
        instance_name="singleton-pstar",
    )


def _partially_fixed_instance() -> B2BInstance:
    """Return a micro-instance with a nontrivial optimum IdleRange(P*)=1."""

    inst = _instance_with_single_meeting_participant()
    return B2BInstance(
        n_business=inst.n_business,
        n_meetings=inst.n_meetings,
        n_tables=inst.n_tables,
        n_total_slots=inst.n_total_slots,
        n_morning_slots=inst.n_morning_slots,
        requested=inst.requested,
        meetings_by_business=inst.meetings_by_business,
        n_meetings_business=inst.n_meetings_business,
        forbidden=inst.forbidden,
        fixed=[0, 4, None, 1],
        precedences=inst.precedences,
        instance_name="pstar-bruteforce",
    )


class IdleRangeParticipantSetTests(unittest.TestCase):
    def test_fixed_schedule_uses_pstar_not_all_participants(self) -> None:
        inst = _instance_with_single_meeting_participant()
        model = B2BSATModel(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
        )
        artifacts = model.build_base_cnf()

        self.assertEqual(model.objective_participants, (0, 1, 2))
        self.assertEqual(artifacts.objective_participants, (0, 1, 2))
        self.assertEqual(artifacts.objective_name, "internal_idle_slot_range_pstar")

        result = B2BMaxSATSolver(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
            backend="rc2",
        ).solve()

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["solver_cost"], 1)
        self.assertEqual(result["objective_participants"], (1, 2, 3))

        stats = result["stats"]
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats.participant_internal_idle_slots, [2, 1, 1, 0])
        self.assertEqual(stats.objective_participants, (0, 1, 2))
        self.assertEqual(stats.objective_participant_ids, (1, 2, 3))
        self.assertEqual(stats.idle_range, 1)
        self.assertEqual(stats.all_participant_idle_range, 2)
        self.assertEqual(result["validation_errors"], [])

    def test_all_three_optimizers_agree_on_pstar_range(self) -> None:
        inst = _instance_with_single_meeting_participant()
        solvers = [
            B2BMaxSATSolver(
                inst,
                precedence_mode="traditional",
                encoding_variant="basic",
                backend="rc2",
            ),
            B2BMultipleSATSolver(
                inst,
                precedence_mode="traditional",
                encoding_variant="basic",
                solver_name="glucose",
            ),
            B2BIncrementalSATSolver(
                inst,
                precedence_mode="traditional",
                encoding_variant="basic",
                solver_name="glucose",
            ),
        ]

        for solver in solvers:
            with self.subTest(solver=type(solver).__name__):
                result = solver.solve()
                self.assertEqual(result["status"], "OPTIMAL")
                self.assertEqual(result["objective_value"], 1)
                self.assertEqual(result["validation_errors"], [])

    def test_optimizer_matches_bruteforce_on_partially_fixed_instance(self) -> None:
        inst = _partially_fixed_instance()
        model = B2BSATModel(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
        )

        feasible_stats = []
        domains = [model.eligible_slots(m) for m in range(inst.n_meetings)]
        for assignment in product(*domains):
            candidate = list(assignment)
            if not model.validate_assignment(candidate):
                feasible_stats.append(model.compute_stats(candidate))

        self.assertTrue(feasible_stats)
        brute_force_optimum = min(stats.idle_range for stats in feasible_stats)
        self.assertEqual(brute_force_optimum, 1)

        result = B2BMaxSATSolver(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
            backend="rc2",
        ).solve()
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["objective_value"], brute_force_optimum)
        self.assertEqual(result["validation_errors"], [])

    def test_singleton_pstar_has_zero_range(self) -> None:
        inst = _instance_with_one_objective_participant()
        result = B2BMaxSATSolver(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
            backend="rc2",
        ).solve()

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["objective_value"], 0)
        stats = result["stats"]
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats.participant_internal_idle_slots, [3, 0, 0])
        self.assertEqual(stats.objective_participants, (0,))
        self.assertEqual(stats.idle_range, 0)
        self.assertEqual(stats.all_participant_idle_range, 3)

    def test_removed_fairness_and_unknown_objective_options_are_rejected(self) -> None:
        inst = _instance_with_single_meeting_participant()
        with self.assertRaises(TypeError):
            B2BSATModel(inst, fairness_limit=1)
        with self.assertRaises(ValueError):
            B2BMaxSATSolver(inst, objective_mode="lexicographic")

    def test_single_meeting_participant_has_no_break_threshold_encoding(self) -> None:
        model = B2BSATModel(
            _instance_with_single_meeting_participant(),
            precedence_mode="traditional",
            encoding_variant="basic",
        )
        artifacts = model.build_base_cnf()

        self.assertEqual(artifacts.sorted_hole_lits_by_participant[3], [])


if __name__ == "__main__":
    unittest.main()
