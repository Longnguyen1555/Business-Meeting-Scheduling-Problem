from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pysat.examples.rc2 import RC2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import B2BInstance, B2BSATModel
from B2B_Instance_CompactV2 import B2BSATModelV2
from B2B_Model_Factory import create_boolean_model
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from Journal_Metrics import evaluate_journal_schedule
from Main import configuration_metadata
from MaxSAT_Solver import B2BMaxSATSolver, decode_scalar_objective_vector
from Multiple_SAT import B2BMultipleSATSolver


def _fixed_v2_instance() -> B2BInstance:
    """Small schedule with non-zero idle values and a precedence relation."""

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
        precedences=[set(), {0}, set(), set()],
        instance_name="compact-v2-fixed",
    )


class CompactV2PipelineTests(unittest.TestCase):
    def test_factory_selects_the_requested_family(self) -> None:
        instance = _fixed_v2_instance()
        self.assertIsInstance(create_boolean_model(instance), B2BSATModel)
        self.assertNotIsInstance(create_boolean_model(instance), B2BSATModelV2)
        self.assertIsInstance(
            create_boolean_model(instance, model_family="compact_v2"),
            B2BSATModelV2,
        )

    def test_new_objectives_match_independent_metrics_and_all_optimizers(
        self,
    ) -> None:
        instance = _fixed_v2_instance()
        for objective_mode in ("max_idle_is", "sla_idle"):
            expected = evaluate_journal_schedule(
                instance,
                [0, 4, 2, 1],
                objective_mode=objective_mode,
                idle_sla_threshold=1,
            ).objective_vector
            vectors = []
            for solver in (
                B2BIncrementalSATSolver(
                    instance,
                    model_family="compact_v2",
                    precedence_encoding="sparse_suffix",
                    precedence_graph="distance_closure",
                    objective_mode=objective_mode,
                    capacity_mode="slot_cluster",
                    idle_sla_threshold=1,
                ),
                B2BMultipleSATSolver(
                    instance,
                    model_family="compact_v2",
                    precedence_encoding="sparse_suffix",
                    precedence_graph="distance_closure",
                    objective_mode=objective_mode,
                    capacity_mode="slot_cluster",
                    idle_sla_threshold=1,
                ),
                B2BMaxSATSolver(
                    instance,
                    model_family="compact_v2",
                    precedence_encoding="sparse_suffix",
                    precedence_graph="distance_closure",
                    objective_mode=objective_mode,
                    capacity_mode="slot_cluster",
                    idle_sla_threshold=1,
                    backend="rc2",
                ),
            ):
                result = solver.solve()
                self.assertEqual(result["status"], "OPTIMAL")
                self.assertEqual(result["validation_errors"], [])
                self.assertEqual(result["objective_vector"], expected)
                vectors.append(result["objective_vector"])
            self.assertEqual(vectors, [expected, expected, expected])

    def test_capacity_cardinality_portfolio_preserves_fixed_optimum(self) -> None:
        instance = _fixed_v2_instance()
        values = []
        for capacity_mode in (
            "meeting",
            "global_cluster",
            "slot_cluster",
            "busy",
            "slot_cluster_busy",
        ):
            for cardinality in (
                "seqcounter",
                "cardnet",
                "totalizer",
                "adaptive",
            ):
                result = B2BMaxSATSolver(
                    instance,
                    model_family="compact_v2",
                    precedence_encoding="pairwise",
                    precedence_graph="direct",
                    objective_mode="ir",
                    capacity_mode=capacity_mode,
                    capacity_cardinality=cardinality,
                    backend="rc2",
                ).solve()
                self.assertEqual(result["status"], "OPTIMAL")
                values.append(result["objective_vector"])
        self.assertEqual(values, [values[0]] * len(values))

    def test_break_counter_and_precedence_encodings_preserve_optimum(self) -> None:
        instance = _fixed_v2_instance()
        for objective_mode in ("bg_d2", "bg_ir_is"):
            counter_values = []
            for counter_mode in ("repeated_seqcounter", "shared_dp"):
                result = B2BMaxSATSolver(
                    instance,
                    model_family="compact_v2",
                    precedence_encoding="sparse_suffix",
                    precedence_graph="distance_closure",
                    objective_mode=objective_mode,
                    bg_counter_mode=counter_mode,
                    backend="rc2",
                ).solve()
                self.assertEqual(result["status"], "OPTIMAL")
                counter_values.append(result["objective_vector"])
            self.assertEqual(counter_values[0], counter_values[1])

        precedence_values = []
        for encoding in ("pairwise", "sparse_suffix", "hybrid_suffix"):
            result = B2BMaxSATSolver(
                instance,
                model_family="compact_v2",
                precedence_encoding=encoding,
                precedence_graph="distance_closure",
                objective_mode="ir",
                backend="rc2",
            ).solve()
            self.assertEqual(result["status"], "OPTIMAL")
            precedence_values.append(result["objective_vector"])
        self.assertEqual(precedence_values, [precedence_values[0]] * 3)

    def test_weighted_scalar_is_a_valid_mixed_radix_vector(self) -> None:
        model = create_boolean_model(
            _fixed_v2_instance(),
            model_family="compact_v2",
            objective_mode="sla_idle",
            idle_sla_threshold=1,
        )
        callbacks: list[int | tuple[int, ...]] = []
        result = B2BMaxSATSolver(
            _fixed_v2_instance(),
            model_family="compact_v2",
            objective_mode="sla_idle",
            idle_sla_threshold=1,
            backend="rc2",
        ).solve(incumbent_callback=callbacks.append)
        self.assertEqual(
            decode_scalar_objective_vector(
                result["solver_cost"], model.build_base_cnf().objective_tiers
            ),
            result["objective_vector"],
        )
        self.assertEqual(callbacks, [result["objective_vector"]])

    def test_uwr_streaming_callback_decodes_a_multitier_vector(self) -> None:
        solver = B2BMaxSATSolver(
            _fixed_v2_instance(),
            model_family="compact_v2",
            objective_mode="sla_idle",
            idle_sla_threshold=1,
            backend="rc2",
        )
        with RC2(solver._build_wcnf()) as reference:
            model = reference.compute()
            cost = int(reference.cost)
        assert model is not None

        class FakeProcess:
            returncode = 30

            def __init__(self) -> None:
                self.stdout = iter(
                    [
                        f"o {cost}\n",
                        "s OPTIMUM FOUND\n",
                        f"v {' '.join(map(str, model))} 0\n",
                    ]
                )

            def wait(self, timeout: float | None = None) -> int:
                return self.returncode

        callbacks: list[int | tuple[int, ...]] = []
        with patch("MaxSAT_Solver.subprocess.Popen", return_value=FakeProcess()):
            result = solver._solve_with_uwrmaxsat(
                Path("uwrmaxsat"),
                False,
                incumbent_callback=callbacks.append,
            )

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(callbacks, [result["objective_vector"]])

    def test_v2_configuration_identity_includes_semantic_factors(self) -> None:
        common = dict(
            solver_name="incremental",
            precedence_encoding="sparse_suffix",
            precedence_graph="distance_closure",
            encoding_variant="imp12+",
            domain_mode="reduced",
            maxsat_backend="rc2",
            sat_backend="cadical",
            model_family="compact_v2",
        )
        meeting = configuration_metadata(**common, capacity_mode="meeting")
        slot = configuration_metadata(**common, capacity_mode="slot_cluster")
        totalizer = configuration_metadata(
            **common,
            capacity_mode="slot_cluster",
            capacity_cardinality="totalizer",
        )
        self.assertEqual(meeting["model_family_display_name"], "Compact V2")
        self.assertEqual(
            len(
                {
                    meeting["configuration_id"],
                    slot["configuration_id"],
                    totalizer["configuration_id"],
                }
            ),
            3,
        )


if __name__ == "__main__":
    unittest.main()
