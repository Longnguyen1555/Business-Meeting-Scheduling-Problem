from __future__ import annotations

import sys
import unittest
from itertools import product
from pathlib import Path
from random import Random

from pysat.solvers import Solver

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import B2BInstance, B2BSATModel
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from Journal_Objectives import LexicographicIdleRC2Oracle
from Journal_Metrics import evaluate_journal_schedule
from MaxSAT_Solver import B2BMaxSATSolver
from Multiple_SAT import B2BMultipleSATSolver


def _lexicographic_tie_instance() -> B2BInstance:
    """Equal-range schedules exist with different aggregate idle."""

    return B2BInstance(
        n_business=4,
        n_meetings=4,
        n_tables=2,
        n_total_slots=4,
        n_morning_slots=2,
        requested=[
            (0, 1, 3),
            (0, 1, 3),
            (2, 3, 3),
            (2, 3, 3),
        ],
        meetings_by_business=[[0, 1], [0, 1], [2, 3], [2, 3]],
        n_meetings_business=[2, 2, 2, 2],
        forbidden=[set() for _ in range(4)],
        fixed=[None] * 4,
        precedences=[set() for _ in range(4)],
        instance_name="journal-lexicographic-tie",
    )


def _fixed_positive_instance() -> B2BInstance:
    """Fixed feasible schedule with objective vector (1, 4)."""

    return B2BInstance(
        n_business=4,
        n_meetings=4,
        n_tables=2,
        n_total_slots=5,
        n_morning_slots=2,
        requested=[
            (0, 1, 3),
            (0, 2, 3),
            (1, 2, 3),
            (3, 0, 3),
        ],
        meetings_by_business=[[0, 1, 3], [0, 2], [1, 2], [3]],
        n_meetings_business=[3, 2, 2, 1],
        forbidden=[set() for _ in range(4)],
        fixed=[0, 4, 2, 1],
        precedences=[set() for _ in range(4)],
        instance_name="journal-fixed-positive",
    )


def _fairness_cap_infeasible_instance() -> B2BInstance:
    """The fixed schedule is base-feasible but has break-group range three."""

    return B2BInstance(
        n_business=5,
        n_meetings=4,
        n_tables=1,
        n_total_slots=7,
        n_morning_slots=3,
        requested=[
            (0, 1, 3),
            (0, 2, 3),
            (0, 3, 3),
            (0, 4, 3),
        ],
        meetings_by_business=[[0, 1, 2, 3], [0], [1], [2], [3]],
        n_meetings_business=[4, 1, 1, 1, 1],
        forbidden=[set() for _ in range(5)],
        fixed=[0, 2, 4, 6],
        precedences=[set() for _ in range(4)],
        instance_name="journal-fairness-cap-infeasible",
    )


def _generated_objective_instance(seed: int) -> B2BInstance:
    """Deterministic micro family spanning feasible and infeasible cases."""

    rng = Random(seed)
    n_business = 4
    n_meetings = 3 + seed % 3
    n_total_slots = 3 + seed % 2
    requested: list[tuple[int, int, int]] = []
    meetings_by_business = [[] for _ in range(n_business)]
    for meeting in range(n_meetings):
        left, right = rng.sample(range(n_business), 2)
        requested.append((left, right, rng.choice([1, 2, 3])))
        meetings_by_business[left].append(meeting)
        meetings_by_business[right].append(meeting)
    forbidden = [
        {
            slot
            for slot in range(n_total_slots)
            if rng.random() < 0.18
        }
        for _ in range(n_business)
    ]
    fixed = [
        rng.randrange(n_total_slots) if rng.random() < 0.3 else None
        for _ in range(n_meetings)
    ]
    precedences = [set() for _ in range(n_meetings)]
    for post in range(n_meetings):
        for predecessor in range(post):
            if rng.random() < 0.22:
                precedences[post].add(predecessor)
    return B2BInstance(
        n_business=n_business,
        n_meetings=n_meetings,
        n_tables=1 + seed % 2,
        n_total_slots=n_total_slots,
        n_morning_slots=n_total_slots // 2,
        requested=requested,
        meetings_by_business=meetings_by_business,
        n_meetings_business=list(map(len, meetings_by_business)),
        forbidden=forbidden,
        fixed=fixed,
        precedences=precedences,
        instance_name=f"journal-generated-{seed}",
    )


def _brute_force_lexicographic_optimum(
    instance: B2BInstance,
) -> tuple[int, int]:
    model = B2BSATModel(
        instance,
        precedence_mode="traditional",
        encoding_variant="basic",
    )
    feasible_values: list[tuple[int, int]] = []
    domains = [
        model.eligible_slots(meeting)
        for meeting in range(instance.n_meetings)
    ]
    for assignment in product(*domains):
        candidate = list(assignment)
        if not model.validate_assignment(candidate):
            stats = model.compute_stats(candidate)
            feasible_values.append(
                (stats.idle_range, stats.total_internal_idle_slots)
            )
    if not feasible_values:
        raise AssertionError("test instance unexpectedly has no feasible schedule")
    return min(feasible_values)


class JournalLexicographicOracleTests(unittest.TestCase):
    def test_dominating_weight_matches_bruteforce_tie_break(self) -> None:
        instance = _lexicographic_tie_instance()
        expected = _brute_force_lexicographic_optimum(instance)
        self.assertEqual(expected, (0, 0))

        result = LexicographicIdleRC2Oracle(
            instance,
            precedence_mode="traditional",
            encoding_variant="basic",
        ).solve()

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["objective_vector"], expected)
        self.assertEqual(result["validation_errors"], [])
        self.assertGreater(result["lexicographic_primary_weight"], 0)

    def test_fixed_positive_objective_vector_and_scalar_cost(self) -> None:
        instance = _fixed_positive_instance()
        oracle = LexicographicIdleRC2Oracle(
            instance,
            precedence_mode="traditional",
            encoding_variant="basic",
        )
        result = oracle.solve()

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["objective_vector"], (1, 4))
        self.assertEqual(
            result["lexicographic_scalar_cost"],
            result["lexicographic_primary_weight"] + 4,
        )
        self.assertEqual(result["validation_errors"], [])

    def test_full_and_reduced_modes_have_same_lexicographic_optimum(self) -> None:
        instance = _lexicographic_tie_instance()
        vectors = []
        for domain_mode in ("full", "reduced"):
            result = LexicographicIdleRC2Oracle(
                instance,
                precedence_mode="traditional",
                encoding_variant="basic",
                domain_mode=domain_mode,
            ).solve()
            self.assertEqual(result["status"], "OPTIMAL")
            self.assertEqual(result["validation_errors"], [])
            vectors.append(result["objective_vector"])
        self.assertEqual(vectors, [(0, 0), (0, 0)])


class JournalObjectiveFamilyTests(unittest.TestCase):
    def test_independent_evaluator_distinguishes_idle_slots_and_groups(self) -> None:
        metrics = evaluate_journal_schedule(
            _fairness_cap_infeasible_instance(),
            [0, 2, 4, 6],
            objective_mode="bg_d2",
        )
        self.assertEqual(metrics.participant_break_groups, (3, 0, 0, 0, 0))
        self.assertEqual(metrics.total_break_groups, 3)
        self.assertEqual(metrics.break_group_range, 3)
        self.assertEqual(metrics.total_internal_idle_slots, 3)
        self.assertFalse(metrics.historical_fairness_cap_satisfied)

    def test_bg_d2_cap_can_make_a_base_feasible_instance_unsat(self) -> None:
        instance = _fairness_cap_infeasible_instance()
        base = B2BMaxSATSolver(
            instance,
            precedence_mode="traditional",
            encoding_variant="basic",
            objective_mode="ir",
            backend="rc2",
        ).solve()
        historical = B2BMaxSATSolver(
            instance,
            precedence_mode="traditional",
            encoding_variant="basic",
            objective_mode="bg_d2",
            backend="rc2",
        ).solve()
        self.assertEqual(base["status"], "OPTIMAL")
        self.assertEqual(historical["status"], "UNSAT")

    def test_encoded_metrics_match_independent_evaluator_exhaustively(self) -> None:
        instance = _lexicographic_tie_instance()
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            model = B2BSATModel(
                instance,
                precedence_mode="traditional",
                encoding_variant="basic",
                objective_mode=objective_mode,
            )
            artifacts = model.build_base_cnf()
            domains = [
                model.eligible_slots(meeting)
                for meeting in range(instance.n_meetings)
            ]
            checked = 0
            with Solver(name="cadical153", bootstrap_with=artifacts.cnf.clauses) as solver:
                for assignment in product(*domains):
                    candidate = list(assignment)
                    if model.validate_assignment(candidate):
                        continue
                    assumptions = [
                        model.x(meeting, slot)
                        for meeting, slot in enumerate(candidate)
                    ]
                    self.assertTrue(solver.solve(assumptions=assumptions))
                    sat_model = solver.get_model()
                    encoded = model.encoded_objective_vector(sat_model)
                    evaluated = evaluate_journal_schedule(
                        instance,
                        candidate,
                        objective_mode=objective_mode,
                    )
                    self.assertEqual(encoded, evaluated.objective_vector)
                    if objective_mode == "bg_d2":
                        self.assertTrue(
                            evaluated.historical_fairness_cap_satisfied
                        )
                    checked += 1
            self.assertGreater(checked, 0)

    def test_three_optimizers_match_bruteforce_on_100_micro_instances(self) -> None:
        for seed in range(100):
            instance = _generated_objective_instance(seed)
            checker = B2BSATModel(instance, domain_mode="full")
            feasible_assignments = [
                list(assignment)
                for assignment in product(
                    range(instance.n_total_slots),
                    repeat=instance.n_meetings,
                )
                if not checker.validate_assignment(list(assignment))
            ]
            for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
                feasible_vectors = []
                for assignment in feasible_assignments:
                    metrics = evaluate_journal_schedule(
                        instance,
                        assignment,
                        objective_mode=objective_mode,
                    )
                    if (
                        objective_mode != "bg_d2"
                        or metrics.historical_fairness_cap_satisfied
                    ):
                        feasible_vectors.append(metrics.objective_vector)
                expected = min(feasible_vectors) if feasible_vectors else None
                solvers = (
                    B2BMaxSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        objective_mode=objective_mode,
                        backend="rc2",
                    ),
                    B2BMultipleSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        objective_mode=objective_mode,
                    ),
                    B2BIncrementalSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        objective_mode=objective_mode,
                    ),
                )
                for solver in solvers:
                    result = solver.solve()
                    with self.subTest(
                        seed=seed,
                        objective_mode=objective_mode,
                        solver=type(solver).__name__,
                    ):
                        if expected is None:
                            self.assertEqual(result["status"], "UNSAT")
                        else:
                            self.assertEqual(result["status"], "OPTIMAL")
                            self.assertEqual(result["objective_vector"], expected)
                            self.assertEqual(result["validation_errors"], [])

    def test_all_boolean_optimizers_and_domains_agree_on_every_mode(self) -> None:
        instance = _fixed_positive_instance()
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            vectors = []
            for domain_mode in ("full", "reduced"):
                solvers = (
                    B2BMaxSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        domain_mode=domain_mode,
                        objective_mode=objective_mode,
                        backend="rc2",
                    ),
                    B2BMultipleSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        domain_mode=domain_mode,
                        objective_mode=objective_mode,
                    ),
                    B2BIncrementalSATSolver(
                        instance,
                        precedence_mode="traditional",
                        encoding_variant="basic",
                        domain_mode=domain_mode,
                        objective_mode=objective_mode,
                    ),
                )
                for solver in solvers:
                    result = solver.solve()
                    self.assertEqual(result["status"], "OPTIMAL")
                    self.assertEqual(result["validation_errors"], [])
                    self.assertEqual(
                        result["proven_objective_vector"],
                        result["objective_vector"],
                    )
                    vectors.append(result["objective_vector"])
            self.assertEqual(vectors, [vectors[0]] * len(vectors))

    def test_weighted_maxsat_cost_reconstructs_every_objective_vector(self) -> None:
        instance = _fixed_positive_instance()
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            result = B2BMaxSATSolver(
                instance,
                precedence_mode="traditional",
                encoding_variant="basic",
                objective_mode=objective_mode,
                backend="rc2",
            ).solve()
            expected_cost = sum(
                value * weight
                for value, weight in zip(
                    result["objective_vector"],
                    result["objective_tier_weights"],
                )
            )
            self.assertEqual(result["solver_cost"], expected_cost)
            self.assertEqual(result["lexicographic_scalar_cost"], expected_cost)
            self.assertEqual(
                result["proven_objective_vector"],
                result["objective_vector"],
            )

    def test_ir_is_records_two_exact_sat_phases(self) -> None:
        instance = _fixed_positive_instance()
        for solver_class in (B2BMultipleSATSolver, B2BIncrementalSATSolver):
            result = solver_class(
                instance,
                precedence_mode="traditional",
                encoding_variant="basic",
                objective_mode="ir_is",
            ).solve()
            self.assertEqual(result["objective_vector"], (1, 4))
            self.assertEqual(result["proven_objective_vector"], (1, 4))
            self.assertEqual(len(result["objective_phase_seconds"]), 2)
            self.assertEqual(len(result["objective_phase_calls"]), 2)


if __name__ == "__main__":
    unittest.main()
