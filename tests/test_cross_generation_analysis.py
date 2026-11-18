from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Cross_Generation_Analysis import (
    displayed_tie,
    aggregate_instance_repetitions,
    canonical_primary_ablation_rows,
    canonical_schedule_row,
    endpoint_formula_equivalence_errors,
    formula_effect_rows,
    population_idle_sd,
    reuse_inventory,
    reuse_row_errors,
    runtime_environment_sensitivity,
    runtime_effect_rows,
    runtime_tables,
    single_run_matrix_rows,
    stress_dataset,
    stress_quality_tables,
)


def row(
    *,
    configuration: str,
    instance: str,
    repetition: int,
    status: str,
    runtime: float,
    memory: float = 100.0,
) -> dict[str, str]:
    return {
        "planned_configuration_id": configuration,
        "instance_content_id": instance,
        "repetition": str(repetition),
        "run_order": str(repetition),
        "status": status,
        "runtime_seconds": str(runtime),
        "peak_memory_mb": str(memory),
    }


class CrossGenerationAnalysisTests(unittest.TestCase):
    @staticmethod
    def sensitivity_rows(instance="i", base=(20, 1, 1), objective=(10, 2, 2)):
        values = []
        for encoding, times in (("base", base), ("objective", objective)):
            for repetition, runtime in enumerate(times, 1):
                value = row(configuration=f"compact_{encoding}_ir", instance=instance,
                            repetition=repetition, status="OPTIMAL", runtime=runtime)
                value.update(instance_sha256=instance, instance_variant="base",
                             archive_dataset="Stress", objective_mode="historical_label",
                             solver_binary_sha256="solver-a", python_version="3.10",
                             pysat_version="1.8", platform="linux", cpu_model="cpu",
                             physical_cpu_cores="4", logical_cpu_cores="8",
                             system_memory_mb="16000")
                values.append(value)
        return values

    def test_environment_restriction_can_reverse_runtime_ranking(self):
        values = self.sensitivity_rows()
        for value in values:
            if value["repetition"] != "1":
                value["solver_binary_sha256"] = value["planned_configuration_id"]
        report = runtime_environment_sensitivity(values)
        self.assertEqual(len(report["comparisons"]), 9)
        result = next(r for r in report["comparisons"] if (r["dataset"], r["objective"]) == ("All-126", "IR"))
        self.assertEqual(result["pooled_median_runtime_ratio"], 0.5)
        self.assertEqual(result["metadata_matched_median_runtime_ratio"], 2)
        self.assertEqual(result["metadata_matched_instances"], 1)
        self.assertEqual(result["metadata_matched_run_pairs"], 1)
        self.assertEqual(result["strata"][0]["paired_runs"][0]["repetition"], 1)

    def test_no_equal_environment_is_unavailable_not_zero(self):
        values = self.sensitivity_rows()
        for value in values:
            value["instance_variant"] = "prec30"
            value["solver_binary_sha256"] = value["planned_configuration_id"]
        report = runtime_environment_sensitivity(values)
        result = next(r for r in report["comparisons"] if (r["dataset"], r["objective"]) == ("Stress", "IR"))
        self.assertEqual(result["all_optimal_instances"], 1)
        self.assertEqual(result["metadata_matched_instances"], 0)
        self.assertIsNone(result["metadata_matched_median_runtime_ratio"])
        self.assertEqual(result["excluded_run_pairs"]["different_metadata"], 3)

    def test_equal_missing_metadata_does_not_create_a_match(self):
        for missing in ("", None, " "):
            with self.subTest(missing=missing):
                values = self.sensitivity_rows()
                for value in values:
                    value["python_version"] = missing
                result = runtime_environment_sensitivity(values)["comparisons"][1]
                self.assertEqual(result["metadata_matched_run_pairs"], 0)
                self.assertEqual(result["excluded_run_pairs"]["missing_metadata"], 3)

    def test_matching_does_not_swap_repetitions(self):
        values = self.sensitivity_rows()
        for value in values:
            repetition = int(value["repetition"])
            if value["planned_configuration_id"] == "compact_objective_ir":
                repetition = repetition % 3 + 1
            value["solver_binary_sha256"] = f"solver-{repetition}"
        result = runtime_environment_sensitivity(values)["comparisons"][1]
        self.assertEqual(result["metadata_matched_run_pairs"], 0)

    def test_matched_ratios_weight_instances_not_run_pairs(self):
        values = self.sensitivity_rows("a", (1, 1, 1), (1, 1, 1))
        extra = self.sensitivity_rows("b", (9, 9, 9), (1, 1, 1))
        for value in extra:
            if value["planned_configuration_id"] == "compact_base_ir" and value["repetition"] != "1":
                value["solver_binary_sha256"] = "different"
        result = runtime_environment_sensitivity(values + extra)["comparisons"][1]
        self.assertEqual(result["metadata_matched_run_pairs"], 4)
        self.assertEqual(result["metadata_matched_instances"], 2)
        self.assertEqual(result["metadata_matched_median_runtime_ratio"], 5)
        self.assertEqual(result["strata"][0]["median_runtime_ratio"], 5)

    def test_mixed_outcomes_do_not_enter_environment_sensitivity(self):
        values = self.sensitivity_rows()
        values[0]["status"] = "TIMEOUT"
        result = runtime_environment_sensitivity(values)["comparisons"][1]
        self.assertEqual(result["all_optimal_instances"], 0)
        self.assertIsNone(result["pooled_median_runtime_ratio"])

    def test_duplicate_sensitivity_runs_are_rejected(self):
        values = self.sensitivity_rows()
        with self.assertRaisesRegex(ValueError, "duplicate sensitivity run"):
            runtime_environment_sensitivity(values + [values[0]])

    def test_displayed_ties_use_the_precision_of_the_table(self) -> None:
        self.assertTrue(displayed_tie(0.8401830476190476, 0.8117964444444444))
        self.assertTrue(displayed_tie(68.332, 68.3205))
        self.assertFalse(displayed_tie(68.332, 68.3205, decimals=2))
        self.assertTrue(displayed_tie(0.3474, 0.3471, decimals=3))

    def test_rounding_boundaries_do_not_create_extra_highlights(self) -> None:
        self.assertTrue(displayed_tie(0.849, 0.811))
        self.assertFalse(displayed_tie(0.851, 0.811))
        self.assertFalse(displayed_tie(0.3476, 0.3471, decimals=3))

    def test_non_best_equal_displayed_values_do_not_match_the_best(self) -> None:
        self.assertTrue(displayed_tie(1.21, 1.24))
        self.assertFalse(displayed_tie(1.21, 0.84))
        self.assertFalse(displayed_tie(1.24, 0.84))

    def test_population_idle_sd_uses_participant_idle_moments(self) -> None:
        selected = {
            "objective_participant_count": "2",
            "total_internal_idle_slots": "4",
            "squared_internal_idle_slots": "10",
        }
        self.assertAlmostEqual(population_idle_sd(selected), 1.0)
        self.assertEqual(population_idle_sd({"objective_participant_count": "0"}), 0.0)

    def test_optimal_run_metrics_retain_timeout_penalties(self) -> None:
        values = [row(configuration="direct_bg_d2", instance="i", repetition=i,
                      status=status, runtime=time, memory=rss)
                  for i, (status, time, rss) in enumerate(
                      [("OPTIMAL", 10, 20), ("TIMEOUT", 100, 900), ("TIMEOUT", 100, 800)], 1)]
        result = aggregate_instance_repetitions(values, timeout_seconds=100,
                                                time_rss_policy="optimal-runs")
        self.assertEqual(result["outcome"], "OPTIMAL")
        self.assertEqual(result["median_runtime_seconds"], 10)
        self.assertEqual(result["median_peak_rss_mb"], 20)
        self.assertEqual(result["median_par2_seconds"], 200)
        self.assertEqual(result["repetitions"], 3)
        self.assertEqual(result["time_rss_repetitions"], 1)
        self.assertFalse(result["all_repetitions_optimal"])
        self.assertEqual([r["status"] for r in values], ["OPTIMAL", "TIMEOUT", "TIMEOUT"])

    def test_two_optimal_runs_supply_their_medians(self) -> None:
        values = [row(configuration="direct_bg_d2", instance="i", repetition=i,
                      status=status, runtime=time, memory=rss)
                  for i, (status, time, rss) in enumerate(
                      [("OPTIMAL", 10, 20), ("TIMEOUT", 100, 900), ("OPTIMAL", 30, 40)], 1)]
        result = aggregate_instance_repetitions(values, timeout_seconds=100,
                                                time_rss_policy="optimal-runs")
        self.assertEqual(result["median_runtime_seconds"], 20)
        self.assertEqual(result["median_peak_rss_mb"], 30)
        self.assertEqual(result["median_par2_seconds"], 30)

    def test_complete_and_unsolved_groups_keep_all_three_runs(self) -> None:
        for status in ("OPTIMAL", "UNSAT", "TIMEOUT"):
            with self.subTest(status=status):
                values = [row(configuration="direct_bg_d2", instance="i", repetition=i,
                              status=status, runtime=time, memory=time * 2)
                          for i, time in enumerate((10, 20, 30), 1)]
                result = aggregate_instance_repetitions(values, timeout_seconds=100,
                                                        time_rss_policy="optimal-runs")
                self.assertEqual(result["median_runtime_seconds"], 20)
                self.assertEqual(result["median_peak_rss_mb"], 40)
                self.assertEqual(result["time_rss_repetitions"], 3)
                self.assertEqual(result["median_par2_seconds"], 200 if status == "TIMEOUT" else 20)

    def test_paired_ratios_exclude_mixed_outcomes_under_optimal_run_policy(self) -> None:
        values = []
        from Cross_Generation_Analysis import CONFIGURATION_MAP
        for config in CONFIGURATION_MAP:
            for rep in (1, 2, 3):
                values.append(row(configuration=config, instance="i", repetition=rep,
                                  status="TIMEOUT" if config == "direct_bg_d2" and rep == 3 else "OPTIMAL",
                                  runtime=100 if rep == 3 else 10))
        summaries, ratios, _ = runtime_tables(values, timeout_seconds=100,
                                              time_rss_policy="optimal-runs")
        direct = next(r for r in summaries if r["objective"] == "BG-d2" and r["encoding"] == "Direct")
        self.assertEqual(direct["OPT"], 1)
        self.assertEqual(direct["median_runtime_seconds"], 10)
        paired = next(r for r in ratios if r["objective"] == "BG-d2" and r["baseline"] == "Direct")
        self.assertEqual(paired["paired_OPT_instances"], 0)

    def test_conference_ir_reruns_map_to_target_repetitions_two_and_three(self) -> None:
        def reusable(configuration: str, repetition: int) -> dict[str, str]:
            return {
                "campaign_id": "source",
                "planned_configuration_id": configuration,
                "repetition": str(repetition),
                "run_key": f"{configuration}-{repetition}",
                "status": "OPTIMAL",
                "campaign_executor_returncode": "0",
                "error_type": "",
                "error_message": "",
                "validation_errors": "",
                "instance": "instance",
                "instance_content_id": "content",
                "instance_sha256": "a" * 64,
                "runtime_seconds": "1",
                "peak_memory_mb": "50",
                "n_vars": "10",
                "n_total_clauses": "20",
                "n_total_literals": "30",
                "objective_vector": "1",
                "runtime_censored": "False",
            }

        compact = [reusable("ir_reference", 1)]
        reruns = [
            reusable("conference_ir_rerun", 1),
            reusable("conference_ir_rerun", 2),
        ]
        inventory = reuse_inventory([], compact, reruns)
        rerun_inventory = [
            row
            for row in inventory
            if row["source_configuration_id"] == "conference_ir_rerun"
        ]
        self.assertEqual(
            {
                (row["source_repetition"], row["target_repetition"])
                for row in rerun_inventory
            },
            {(1, 2), (2, 3)},
        )
        self.assertTrue(all(row["accepted"] for row in rerun_inventory))

    def test_stress_dataset_uses_precedence_level(self) -> None:
        self.assertEqual(stress_dataset({"instance_variant": "prec30"}), "Stress")
        self.assertEqual(
            stress_dataset({"instance_variant": "prec60"}), "Stress-high"
        )
        with self.assertRaises(ValueError):
            stress_dataset({"instance_variant": "base"})

    def test_single_run_matrix_maps_one_validated_row_per_cell(self) -> None:
        source_configurations = (
            "published_2022_bg_d2",
            "published_2022_ir",
            "published_2022_ir_im_is",
            "bg_reference",
            "ir_reference",
            "irimis_reference",
            "compact_acf_bg_d2",
            "compact_cdf_ir",
            "compact_cdf_ir_im_is",
        )
        rows = []
        for configuration in source_configurations:
            rows.append(
                {
                    "planned_configuration_id": configuration,
                    "repetition": "1",
                    "run_key": configuration,
                    "status": "OPTIMAL",
                    "campaign_executor_returncode": "0",
                    "error_type": "",
                    "error_message": "",
                    "validation_errors": "",
                    "instance_content_id": "b2b-content",
                    "instance_sha256": "a" * 64,
                    "runtime_seconds": "2.5",
                    "peak_memory_mb": "100",
                    "n_vars": "10",
                    "n_total_clauses": "20",
                    "n_total_literals": "30",
                    "objective_vector": "1",
                    "runtime_censored": "False",
                }
            )
        mapped, validation = single_run_matrix_rows(
            rows,
            [],
            expected_instances=1,
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["row_count"], 9)
        self.assertEqual(
            {value["planned_configuration_id"] for value in mapped},
            {
                "direct_bg_d2",
                "direct_ir",
                "direct_ir_im_is",
                "compact_base_bg_d2",
                "compact_base_ir",
                "compact_base_ir_im_is",
                "compact_objective_bg_d2",
                "compact_objective_ir",
                "compact_objective_ir_im_is",
            },
        )

    def test_reuse_validation_ignores_provenance_but_rejects_failed_rows(self) -> None:
        candidate = {
            "status": "OPTIMAL",
            "campaign_executor_returncode": "0",
            "error_type": "",
            "error_message": "",
            "validation_errors": "",
            "instance_content_id": "b2b-content",
            "instance_sha256": "a" * 64,
            "runtime_seconds": "2.5",
            "peak_memory_mb": "100",
            "n_vars": "10",
            "n_total_clauses": "20",
            "n_total_literals": "30",
            "objective_vector": "1|2|3",
            "runtime_censored": "False",
            "git_commit": "different-commit",
            "python_version": "different-version",
            "solver_binary_sha256": "different-build",
        }
        self.assertEqual(reuse_row_errors(candidate), [])
        candidate["status"] = "ERROR"
        candidate["campaign_executor_returncode"] = "1"
        self.assertIn("nonterminal_status=ERROR", reuse_row_errors(candidate))
        self.assertIn("nonzero_executor_returncode", reuse_row_errors(candidate))

    def test_timeout_reuse_requires_censoring_marker(self) -> None:
        candidate = {
            "status": "TIMEOUT",
            "campaign_executor_returncode": "0",
            "error_type": "",
            "error_message": "",
            "validation_errors": "",
            "instance_content_id": "b2b-content",
            "instance_sha256": "a" * 64,
            "runtime_seconds": "7200",
            "peak_memory_mb": "100",
            "n_vars": "10",
            "n_total_clauses": "20",
            "n_total_literals": "30",
            "runtime_censored": "False",
        }
        self.assertIn("uncensored_timeout", reuse_row_errors(candidate))
        candidate["runtime_censored"] = "True"
        self.assertEqual(reuse_row_errors(candidate), [])

    def test_repetition_aggregation_applies_par2_before_instance_aggregation(self) -> None:
        summary = aggregate_instance_repetitions(
            [
                row(configuration="direct_bg_d2", instance="i", repetition=1, status="OPTIMAL", runtime=10),
                row(configuration="direct_bg_d2", instance="i", repetition=2, status="TIMEOUT", runtime=100),
                row(configuration="direct_bg_d2", instance="i", repetition=3, status="TIMEOUT", runtime=100),
            ],
            timeout_seconds=100,
        )
        self.assertEqual(summary["outcome"], "OPTIMAL")
        self.assertEqual(summary["median_runtime_seconds"], 100)
        self.assertEqual(summary["median_par2_seconds"], 200)

    def test_canonical_schedule_uses_smallest_optimal_repetition(self) -> None:
        rows = [
            row(configuration="compact_objective_ir", instance="i", repetition=3, status="OPTIMAL", runtime=1),
            row(configuration="compact_objective_ir", instance="i", repetition=1, status="TIMEOUT", runtime=100),
            row(configuration="compact_objective_ir", instance="i", repetition=2, status="OPTIMAL", runtime=9),
        ]
        self.assertEqual(canonical_schedule_row(rows)["repetition"], "2")

    def test_runtime_ratio_uses_instance_medians_and_only_paired_optima(self) -> None:
        rows = []
        configurations = (
            "direct_bg_d2",
            "compact_base_bg_d2",
            "compact_objective_bg_d2",
            "direct_ir",
            "compact_base_ir",
            "compact_objective_ir",
            "direct_ir_im_is",
            "compact_base_ir_im_is",
            "compact_objective_ir_im_is",
        )
        for configuration in configurations:
            for repetition, runtime in enumerate((1.0, 2.0, 9.0), start=1):
                rows.append(
                    row(
                        configuration=configuration,
                        instance="i",
                        repetition=repetition,
                        status="OPTIMAL",
                        runtime=(runtime * 2 if configuration == "compact_base_bg_d2" else runtime),
                    )
                )
        _, ratios, _ = runtime_tables(rows, timeout_seconds=100)
        target = next(
            value
            for value in ratios
            if value["objective"] == "BG-d2"
            and value["encoding"] == "Compact-Base"
            and value["baseline"] == "Direct"
        )
        self.assertEqual(target["paired_OPT_instances"], 1)
        self.assertEqual(target["paired_median_runtime_ratio"], 2.0)

        effects = runtime_effect_rows(rows, timeout_seconds=100)
        speedup = next(
            value
            for value in effects
            if value["objective"] == "BG-d2"
            and value["left_encoding"] == "Direct"
            and value["right_encoding"] == "Compact-Base"
        )
        self.assertEqual(speedup["runtime_speed_ratio_left_over_right"], 0.5)

    def test_formula_effect_rows_use_paired_instance_reductions(self) -> None:
        rows = []
        clauses = {"Direct": 100, "Compact-Base": 80, "Compact-Objective": 60}
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            for encoding, count in clauses.items():
                rows.append(
                    {
                        "objective": objective,
                        "encoding": encoding,
                        "instance_content_id": "i",
                        "n_total_clauses": count,
                    }
                )
        effects = formula_effect_rows(rows)
        target = next(
            value
            for value in effects
            if value["objective"] == "BG-d2"
            and value["left_encoding"] == "Compact-Base"
            and value["right_encoding"] == "Compact-Objective"
        )
        self.assertEqual(target["clause_reduction_percent"], 25.0)

    def test_canonical_ablation_replaces_duplicate_endpoint(self) -> None:
        required = {
            "repetition": "1",
            "run_key": "test",
            "status": "OPTIMAL",
            "campaign_executor_returncode": "0",
            "error_type": "",
            "error_message": "",
            "validation_errors": "",
            "instance_content_id": "i",
            "instance_sha256": "a" * 64,
            "runtime_seconds": "2.5",
            "peak_memory_mb": "100",
            "n_vars": "10",
            "n_total_clauses": "20",
            "n_total_literals": "30",
            "objective_vector": "1",
            "runtime_censored": "False",
        }
        duplicate = {**required, "planned_configuration_id": "bg_acf"}
        canonical = {
            **required,
            "planned_configuration_id": "compact_objective_bg_d2",
            "runtime_seconds": "1.25",
        }
        selected = canonical_primary_ablation_rows([duplicate], [canonical])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["planned_configuration_id"], "bg_acf")
        self.assertEqual(selected[0]["runtime_seconds"], "1.25")

    def test_endpoint_formula_audit_detects_deterministic_mismatch(self) -> None:
        matched = {
            "planned_configuration_id": "compact_objective_ir",
            "instance_content_id": "i",
            "instance_sha256": "a" * 64,
            "n_vars": "10",
            "n_total_clauses": "20",
            "n_total_literals": "30",
        }
        duplicate = {
            **matched,
            "planned_configuration_id": "ir_cdf",
        }
        self.assertEqual(endpoint_formula_equivalence_errors([duplicate], [matched]), [])
        duplicate["n_total_clauses"] = "21"
        self.assertTrue(endpoint_formula_equivalence_errors([duplicate], [matched]))

    def test_stress_quality_uses_joint_optimal_set(self) -> None:
        rows = []
        for configuration, total in (
            ("compact_objective_ir", 8),
            ("compact_objective_ir_im_is", 5),
        ):
            rows.append(
                {
                    "planned_configuration_id": configuration,
                    "instance_content_id": "i",
                    "instance_variant": "prec30",
                    "status": "OPTIMAL",
                    "idle_range_pstar": "1",
                    "maximum_internal_idle_slots": "2",
                    "total_internal_idle_slots": str(total),
                }
            )
        comparisons, membership = stress_quality_tables(rows)
        total_idle = next(
            value
            for value in comparisons
            if value["dataset"] == "Stress"
            and value["measure"] == "total_internal_idle_slots"
        )
        self.assertEqual(total_idle["instances"], 1)
        self.assertEqual(total_idle["lower"], 1)
        self.assertTrue(membership[0]["included"])


if __name__ == "__main__":
    unittest.main()
