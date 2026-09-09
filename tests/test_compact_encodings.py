from __future__ import annotations

import sys
import unittest
from itertools import product
from pathlib import Path

from pysat.formula import CNF
from pysat.solvers import Solver

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import B2BInstance, B2BSATModel
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from Journal_Experiment import build_plan, read_config, resolve_datasets
from Main import (
    InstanceSpec,
    benchmark_configurations,
    parse_args,
    selected_solvers,
)
from Journal_Metrics import evaluate_journal_schedule
from MaxSAT_Solver import B2BMaxSATSolver
from Multiple_SAT import B2BMultipleSATSolver


def _certified_fixed_instance() -> B2BInstance:
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
        instance_name="compact-certified-fixed",
    )


def _no_certificate_instance() -> B2BInstance:
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
        instance_name="compact-no-zero-break-certificate",
    )


def _large_collision_instance() -> B2BInstance:
    meetings = list(range(6))
    return B2BInstance(
        n_business=7,
        n_meetings=6,
        n_tables=1,
        n_total_slots=6,
        n_morning_slots=3,
        requested=[(0, participant, 3) for participant in range(1, 7)],
        meetings_by_business=[meetings, *[[meeting] for meeting in meetings]],
        n_meetings_business=[6, 1, 1, 1, 1, 1, 1],
        forbidden=[set() for _ in range(7)],
        fixed=[None] * len(meetings),
        precedences=[set() for _ in meetings],
        instance_name="compact-large-collision",
    )


class CompactObjectiveEncodingTests(unittest.TestCase):
    def test_commander_amo_truth_table_is_exact(self) -> None:
        model = B2BSATModel(_large_collision_instance())
        cnf = CNF()
        inputs = [
            model.vpool.id(("commander-test-input", index))
            for index in range(8)
        ]
        model._add_commander_atmost_one(cnf, inputs)

        with Solver(name="cadical153", bootstrap_with=cnf.clauses) as solver:
            for values in product((False, True), repeat=len(inputs)):
                assumptions = [
                    literal if value else -literal
                    for literal, value in zip(inputs, values)
                ]
                self.assertEqual(
                    solver.solve(assumptions=assumptions),
                    sum(values) <= 1,
                )

    def test_adaptive_commander_preserves_all_objective_optima(self) -> None:
        instance = _large_collision_instance()
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            results = [
                B2BMaxSATSolver(
                    instance,
                    precedence_mode="traditional",
                    encoding_variant="basic",
                    objective_mode=objective_mode,
                    compact_encoding="optimized",
                    collision_amo_encoding=collision_amo,
                    backend="rc2",
                ).solve()
                for collision_amo in ("pairwise", "adaptive_commander")
            ]
            with self.subTest(objective_mode=objective_mode):
                self.assertEqual(
                    [result["status"] for result in results],
                    ["OPTIMAL", "OPTIMAL"],
                )
                self.assertEqual(results[0]["objective_vector"], results[1]["objective_vector"])
                self.assertEqual(results[0]["validation_errors"], [])
                self.assertEqual(results[1]["validation_errors"], [])

    def test_adaptive_commander_records_structural_tradeoff(self) -> None:
        instance = _large_collision_instance()
        pairwise = B2BSATModel(
            instance,
            objective_mode="bg_d2",
            compact_encoding="optimized",
            collision_amo_encoding="pairwise",
        ).build_base_cnf()
        commander = B2BSATModel(
            instance,
            objective_mode="bg_d2",
            compact_encoding="optimized",
            collision_amo_encoding="adaptive_commander",
        ).build_base_cnf()

        self.assertEqual(pairwise.collision_amo_pairwise_group_count, 6)
        self.assertEqual(pairwise.collision_amo_commander_group_count, 0)
        self.assertEqual(commander.collision_amo_pairwise_group_count, 0)
        self.assertEqual(commander.collision_amo_commander_group_count, 6)
        self.assertEqual(commander.collision_amo_cutoff, 6)
        self.assertEqual(commander.collision_amo_commander_group_size, 4)
        self.assertEqual(commander.collision_amo_max_group_size, 6)
        self.assertEqual(commander.collision_amo_commander_variable_count, 12)
        self.assertLess(commander.n_clauses, pairwise.n_clauses)
        self.assertGreater(commander.n_vars, pairwise.n_vars)

    def test_every_compact_encoding_matches_decoded_cost_exhaustively(self) -> None:
        instance = _no_certificate_instance()
        presets = (
            "reference",
            "certified_bg",
            "demand_driven",
            "shared_counter",
            "direct_range_soft",
            "optimized",
        )
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            for preset in presets:
                model = B2BSATModel(
                    instance,
                    precedence_mode="traditional",
                    encoding_variant="basic",
                    objective_mode=objective_mode,
                    compact_encoding=preset,
                )
                artifacts = model.build_base_cnf()
                domains = [
                    model.eligible_slots(meeting)
                    for meeting in range(instance.n_meetings)
                ]
                checked = 0
                with Solver(
                    name="cadical153",
                    bootstrap_with=artifacts.cnf.clauses,
                ) as solver:
                    for assignment in product(*domains):
                        candidate = list(assignment)
                        if model.validate_assignment(candidate):
                            continue
                        assumptions = [
                            model.x(meeting, slot)
                            for meeting, slot in enumerate(candidate)
                        ]
                        self.assertTrue(solver.solve(assumptions=assumptions))
                        encoded = model.encoded_objective_vector(
                            solver.get_model()
                        )
                        expected = evaluate_journal_schedule(
                            instance,
                            candidate,
                            objective_mode=objective_mode,
                        ).objective_vector
                        self.assertEqual(encoded, expected)
                        checked += 1
                with self.subTest(
                    objective_mode=objective_mode,
                    compact_encoding=preset,
                ):
                    self.assertGreater(checked, 0)

    def test_all_maxsat_presets_preserve_every_objective_vector(self) -> None:
        instance = _certified_fixed_instance()
        presets = (
            "reference",
            "certified_bg",
            "demand_driven",
            "shared_counter",
            "direct_range_soft",
            "optimized",
        )
        for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is"):
            results = [
                B2BMaxSATSolver(
                    instance,
                    precedence_mode="traditional",
                    encoding_variant="basic",
                    objective_mode=objective_mode,
                    compact_encoding=preset,
                    domain_mode=domain_mode,
                    backend="rc2",
                ).solve()
                for domain_mode in ("full", "reduced")
                for preset in presets
            ]
            with self.subTest(objective_mode=objective_mode):
                self.assertTrue(
                    all(result["status"] == "OPTIMAL" for result in results)
                )
                self.assertTrue(
                    all(result["validation_errors"] == [] for result in results)
                )
                vectors = [result["objective_vector"] for result in results]
                self.assertEqual(vectors, [vectors[0]] * len(vectors))

    def test_certified_bg_uses_proved_branch_and_falls_back_safely(self) -> None:
        certified = B2BSATModel(
            _certified_fixed_instance(),
            objective_mode="bg_d2",
            compact_encoding="certified_bg",
        ).build_base_cnf()
        fallback = B2BSATModel(
            _no_certificate_instance(),
            objective_mode="bg_d2",
            compact_encoding="certified_bg",
        ).build_base_cnf()

        self.assertEqual(certified.zero_break_branch, "certified")
        self.assertIsNotNone(certified.zero_break_certificate_participant)
        self.assertEqual(certified.break_group_range_lits, [])
        self.assertTrue(
            all(
                not values
                for values in certified.break_group_threshold_lits_by_participant
            )
        )
        self.assertEqual(fallback.zero_break_branch, "general_fallback")
        self.assertIsNone(fallback.zero_break_certificate_participant)
        self.assertTrue(fallback.break_group_range_lits)

    def test_shared_counter_outputs_are_exact_in_both_directions(self) -> None:
        model = B2BSATModel(
            _no_certificate_instance(),
            objective_mode="bg_d2",
            compact_encoding="shared_counter",
        )
        cnf = CNF()
        inputs = [
            model.vpool.id(("counter-test-input", index))
            for index in range(5)
        ]
        outputs = model._add_shared_exact_cardinality_thresholds(
            cnf,
            inputs,
            participant=0,
            upper_bound=4,
        )
        self.assertEqual(len(outputs), 4)

        with Solver(name="cadical153", bootstrap_with=cnf.clauses) as solver:
            for values in product((False, True), repeat=len(inputs)):
                assumptions = [
                    literal if value else -literal
                    for literal, value in zip(inputs, values)
                ]
                self.assertTrue(solver.solve(assumptions=assumptions))
                positives = {
                    literal for literal in solver.get_model() if literal > 0
                }
                count = sum(values)
                self.assertEqual(
                    tuple(output in positives for output in outputs),
                    tuple(count >= amount for amount in range(1, 5)),
                )

        empty = model._add_shared_exact_cardinality_thresholds(
            CNF(),
            [],
            participant=1,
            upper_bound=0,
        )
        self.assertEqual(empty, [])

    def test_each_structural_factor_records_its_effect(self) -> None:
        instance = _certified_fixed_instance()
        reference_ir = B2BSATModel(
            instance,
            objective_mode="ir",
            compact_encoding="reference",
        ).build_base_cnf()
        demand_ir = B2BSATModel(
            instance,
            objective_mode="ir",
            compact_encoding="demand_driven",
        ).build_base_cnf()
        direct_model = B2BSATModel(
            instance,
            objective_mode="ir",
            compact_encoding="direct_range_soft",
        )
        direct_ir = direct_model.build_base_cnf()
        shared_bg = B2BSATModel(
            instance,
            objective_mode="bg_ir_is",
            compact_encoding="shared_counter",
        ).build_base_cnf()

        self.assertGreater(demand_ir.occupancy_alias_count, 0)
        self.assertLess(demand_ir.n_vars, reference_ir.n_vars)
        self.assertGreater(direct_ir.direct_range_soft_clause_count, 0)
        self.assertLess(direct_ir.n_vars, reference_ir.n_vars)
        direct_wcnf = direct_model.build_wcnf()
        self.assertEqual(
            sum(len(clause) == 2 for clause in direct_wcnf.soft),
            direct_ir.direct_range_soft_clause_count,
        )
        self.assertGreater(shared_bg.shared_counter_state_count, 0)

    def test_nonreference_configuration_identity_is_explicit(self) -> None:
        from Main import configuration_metadata

        metadata = configuration_metadata(
            solver_name="maxsat",
            precedence_encoding="sparse_suffix",
            precedence_graph="distance_closure",
            encoding_variant="imp12+",
            domain_mode="reduced",
            maxsat_backend="uwrmaxsat",
            sat_backend="cadical",
            objective_mode="ir_is",
            compact_encoding="optimized",
        )
        self.assertEqual(metadata["factor_c"], "OptimizedABCD")
        self.assertIn("__c-optimized", metadata["configuration_id"])
        self.assertTrue(metadata["configuration_label"].endswith("-CABCD"))

        f_metadata = configuration_metadata(
            solver_name="maxsat",
            precedence_encoding="sparse_suffix",
            precedence_graph="distance_closure",
            encoding_variant="imp12+",
            domain_mode="reduced",
            maxsat_backend="uwrmaxsat",
            sat_backend="cadical",
            objective_mode="ir_is",
            compact_encoding="optimized",
            collision_amo="adaptive_commander",
        )
        self.assertEqual(f_metadata["factor_amo"], "AdaptiveCommander")
        self.assertIn("__amo-adaptive_commander", f_metadata["configuration_id"])
        self.assertTrue(f_metadata["configuration_label"].endswith("-FAMOAC"))

    def test_nonunit_range_penalties_are_rejected_by_sat_optimizers(self) -> None:
        instance = _certified_fixed_instance()
        for solver_class in (B2BMultipleSATSolver, B2BIncrementalSATSolver):
            with self.subTest(solver=solver_class.__name__):
                with self.assertRaisesRegex(ValueError, "MaxSAT-only"):
                    solver_class(
                        instance,
                        objective_mode="ir_is",
                        compact_encoding="direct_range_soft",
                    )

    def test_main_all_expansion_excludes_maxsat_only_sat_cells(self) -> None:
        instance = InstanceSpec(
            path=Path("micro.dzn"),
            instance_name="micro",
            content_id="micro-id",
            sha256="0" * 64,
            family="original",
            variant="original",
            has_precedence=False,
            source_alias_count=1,
            source_alias_paths="micro.dzn",
        )
        common = [
            "--domain-mode",
            "reduced",
            "--precedence-encoding",
            "pairwise",
            "--precedence-graph",
            "direct",
            "--objective-mode",
            "ir_is",
            "--compact-encoding",
            "all",
        ]
        maxsat_args = parse_args(["--solver", "maxsat", *common])
        multiple_args = parse_args(["--solver", "multiple", *common])
        maxsat = benchmark_configurations(
            maxsat_args,
            instance,
            selected_solvers(maxsat_args.solver),
        )
        multiple = benchmark_configurations(
            multiple_args,
            instance,
            selected_solvers(multiple_args.solver),
        )

        self.assertEqual(len(maxsat), 6)
        self.assertEqual(len(multiple), 4)
        self.assertNotIn(
            "direct_range_soft",
            {configuration.compact_encoding for configuration in multiple},
        )
        self.assertNotIn(
            "optimized",
            {configuration.compact_encoding for configuration in multiple},
        )

        maxsat_all_amo_args = parse_args(
            ["--solver", "maxsat", "--collision-amo", "all", *common]
        )
        maxsat_all_amo = benchmark_configurations(
            maxsat_all_amo_args,
            instance,
            selected_solvers(maxsat_all_amo_args.solver),
        )
        self.assertEqual(len(maxsat_all_amo), 12)
        self.assertEqual(
            {configuration.collision_amo for configuration in maxsat_all_amo},
            {"pairwise", "adaptive_commander"},
        )

    def test_frozen_compact_campaign_counts_are_exact(self) -> None:
        expected_counts = {
            "compact_smoke.json": 168,
            "compact_ablation.json": 5292,
            "compact_f_smoke.json": 72,
            "compact_f_ablation.json": 2268,
        }
        for filename, expected in expected_counts.items():
            path = PROJECT_ROOT / "journal_configs" / filename
            config = read_config(path)
            plan = build_plan(config, resolve_datasets(config))
            with self.subTest(filename=filename):
                self.assertEqual(plan["job_count"], expected)


if __name__ == "__main__":
    unittest.main()
