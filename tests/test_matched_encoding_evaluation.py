from __future__ import annotations

import csv
import copy
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from Matched_Encoding_Evaluation import (
    execution_identity, prepare_evaluation, recover_assignment, seed_shard, verify_bundle,
    run_shard, validate_source_row, write_jsonl,
)
from Journal_Experiment import _read_jsonl, build_plan, materialize_results, read_config, resolve_datasets
from Validate_Journal_Run import validate_campaign
from Cross_Generation_Analysis import (
    aggregate_instance_repetitions, analyze_matched_campaign, runtime_effect_rows,
    validate_matched_matrix,
)


class MatchedEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="matched_evaluation_test_")
        cls.bundle = Path(cls.temporary.name) / "bundle"
        cls.report = prepare_evaluation(cls.bundle)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_inventory_is_4960_plus_1142_without_duplicate_cells(self):
        self.assertEqual(self.report["planned_jobs"], 6102)
        self.assertEqual(self.report["reused_jobs"], 4960)
        self.assertEqual(self.report["remaining_by_dataset"], {"All-126": 378, "Stress": 420, "Stress-high": 344})
        with (self.bundle / "remaining_runs.csv").open(newline="") as stream:
            remaining = list(csv.DictReader(stream))
        self.assertEqual(len({row["target_run_key"] for row in remaining}), 1142)
        self.assertEqual(Counter(row["target_configuration_id"] for row in remaining), {
            "direct_bg_d2": 100, "direct_ir": 100, "direct_ir_im_is": 132,
            "compact_base_bg_d2": 326, "compact_base_ir_im_is": 452,
            "compact_objective_ir_im_is": 32,
        })
        config = read_config(self.bundle / "campaign.json")
        plan = build_plan(config, resolve_datasets(config))
        self.assertEqual(plan["plan_sha256"], self.report["plan_sha256"])
        self.assertEqual([self.report["shards"][str(index)]["remaining_jobs"] for index in range(3)], [382, 379, 381])

    def test_malformed_per_cell_provenance_is_rejected(self):
        config = read_config(self.bundle / "campaign.json")
        key = next(iter(config["reuse_cells"]))
        config["reuse_cells"][key]["source_row_sha256"] = "invalid"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_plan(config, resolve_datasets(config))

    def test_three_shards_are_disjoint_and_resume_keeps_records(self):
        keys = set()
        for index, expected in enumerate((1670, 1646, 1644)):
            destination = seed_shard(self.bundle, index)
            records = _read_jsonl(destination / "raw/results.jsonl")
            self.assertEqual(len(records), expected)
            these = {record["run_key"] for record in records}
            self.assertFalse(keys & these)
            keys.update(these)
            seed_shard(self.bundle, index)
            self.assertEqual(_read_jsonl(destination / "raw/results.jsonl"), records)
            errors, _ = validate_campaign(destination, allow_dirty=True, allow_incomplete=True, allow_environment_drift=True)
            self.assertEqual(errors, ["missing environment.json"])
        self.assertEqual(len(keys), 4960)

    def test_assignment_recovery_and_indexing(self):
        self.assertEqual(recover_assignment({"schedule_by_slot": "1:M2 | 2:M1"}, 2), [1, 0])
        self.assertEqual(recover_assignment({"planned_configuration_id": "published_2022_bg_d2", "assignment": "0,1"}, 2), [0, 1])
        with self.assertRaises(ValueError):
            recover_assignment({"schedule_by_slot": "1:M1 | 2:M1"}, 2)
        with self.assertRaises(ValueError):
            recover_assignment({"assignment": "1"}, 2)

    def test_prepare_plan_only_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "uncreated"
            report = prepare_evaluation(destination, plan_only=True)
            self.assertEqual(report["remaining_jobs"], 1142)
            self.assertFalse(destination.exists())

    def test_execution_identity_does_not_depend_on_campaign_or_rep_labels(self):
        row = {"instance_sha256": "a" * 64, "run_started_utc": "2026-10-23T00:00:00Z", "hostname": "vm", "solver_binary_sha256": "b" * 64}
        original = execution_identity(row, "compact_base_ir")
        row.update({"campaign_id": "copy", "repetition": "3"})
        self.assertEqual(execution_identity(row, "compact_base_ir"), original)

    def test_tampered_bundle_is_rejected(self):
        with patch("Matched_Encoding_Evaluation.file_sha256", return_value="invalid"):
            with self.assertRaisesRegex(ValueError, "changed"):
                verify_bundle(self.bundle)

    def test_incomplete_campaign_cannot_update_latex(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            source = self.bundle / "shard-0-of-3/normalized/detailed.csv"
            seed_shard(self.bundle, 0)
            latex = output / "rows.tex"
            latex.write_text("preserve manuscript rows")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                analyze_matched_campaign(source, output_dir=output / "analysis", latex_output=latex)
            self.assertEqual(latex.read_text(), "preserve manuscript rows")
            self.assertFalse((output / "analysis").exists())

    def test_partial_reruns_are_materialized_without_source_writes(self):
        for code in ("m05", "m06"):
            derived = self.bundle / "recovered_sources" / code / "normalized/detailed.csv"
            with derived.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 68)
            self.assertFalse((ROOT / "outputs/journal-rerun/main-stress-sessions" / code / "normalized/detailed.csv").exists())

    def test_interrupted_seeding_restores_only_missing_records(self):
        destination = seed_shard(self.bundle, 0)
        raw = destination / "raw/results.jsonl"
        original = _read_jsonl(raw)
        try:
            write_jsonl(raw, original[1:])
            seed_shard(self.bundle, 0)
            restored = _read_jsonl(raw)
            self.assertEqual(len(restored), len(original))
            self.assertEqual(restored[:-1], original[1:])
            self.assertEqual(restored[-1], original[0])
        finally:
            write_jsonl(raw, original)
            materialize_results(destination)

    def test_worker_checks_preflight_then_resumes_existing_runner(self):
        pin = "29c51abeabd43015c75a8e6ee789fdbb9aa2803096d2cac536ec60bd516ab4a7"
        with patch("Matched_Encoding_Evaluation.machine_profile_errors", return_value=[]), \
             patch("Matched_Encoding_Evaluation.resolve_uwrmaxsat_binary", return_value=Path(__file__)), \
             patch("Matched_Encoding_Evaluation.executable_sha256", return_value=pin), \
             patch("Journal_Experiment.main", return_value=0) as runner:
            result = run_shard(self.bundle, 0, binary="test-only-mock", binary_sha256=pin, allow_dirty=True)
        self.assertEqual(result, 0)
        arguments = runner.call_args.args[0]
        self.assertIn("--resume", arguments)
        self.assertEqual(arguments[arguments.index("--shard-index") + 1], "0")

    def test_invalid_source_rows_are_not_reused(self):
        row = next(record["row"] for record in _read_jsonl(self.bundle / "reused_records.jsonl") if record["row"]["status"] == "OPTIMAL")
        with self.assertRaises(ValueError):
            validate_source_row({**row, "error_type": "ExecutorFailure"})
        with self.assertRaises(ValueError):
            validate_source_row({**row, "sat_result": "UNSAT"})
        with self.assertRaises(ValueError):
            validate_source_row({**row, "timeout_seconds": "60"})

    def test_paired_effects_exclude_an_instance_with_one_timeout(self):
        rows = []
        for objective in ("bg_d2", "ir", "ir_im_is"):
            for encoding in ("direct", "compact_base", "compact_objective"):
                for repetition in (1, 2, 3):
                    status = "TIMEOUT" if encoding == "compact_base" and repetition == 3 else "OPTIMAL"
                    rows.append({"planned_configuration_id": f"{encoding}_{objective}", "instance_content_id": "i", "repetition": str(repetition), "status": status, "runtime_seconds": "7200" if status == "TIMEOUT" else "1", "peak_memory_mb": "50"})
        effects = runtime_effect_rows(rows)
        self.assertTrue(effects)
        self.assertTrue(all("Compact-Base" not in (row["left_encoding"], row["right_encoding"]) for row in effects))
        summary = aggregate_instance_repetitions(rows[:3])
        self.assertEqual(summary["median_par2_seconds"], 1)

    def test_complete_analysis_writes_three_run_tables_and_latex(self):
        """Synthetic repetitions stay inside this test's temporary directory."""
        from Main import write_detailed_csv
        plan = json.loads((self.bundle / "plan.json").read_text())
        existing = {record["run_key"]: record["row"] for record in _read_jsonl(self.bundle / "reused_records.jsonl")}
        by_pair = {(row["planned_configuration_id"], row["instance_sha256"]): row for row in existing.values()}
        complete = []
        for job in plan["jobs"]:
            row = copy.deepcopy(existing.get(job["run_key"]) or by_pair[(job["planned_configuration_id"], job["instance_sha256"])])
            row.update({"run_key": job["run_key"], "repetition": job["repetition"], "run_order": job["run_order"],
                        "run_started_utc": f"test-only-{job['run_key']}", "hostname": "test-only-fixture"})
            complete.append(row)
        chosen_sha = next(row["instance_sha256"] for row in complete if row["planned_configuration_id"] == "compact_objective_ir" and row["instance_variant"] == "original" and row["status"] == "OPTIMAL")
        for row in complete:
            if row["planned_configuration_id"] == "compact_objective_ir" and row["instance_sha256"] == chosen_sha:
                row["runtime_seconds"] = (1, 3, 9)[int(row["repetition"]) - 1]
        with tempfile.TemporaryDirectory(prefix="synthetic_matched_analysis_") as directory:
            root = Path(directory)
            source = root / "test-only-detailed.csv"
            write_detailed_csv(source, complete)
            report = analyze_matched_campaign(source, output_dir=root / "analysis", latex_output=root / "rows.tex")
            self.assertTrue(report["production_campaign_complete"])
            self.assertEqual(report["aggregation"], "three_repetitions")
            with (root / "analysis/repetition_outcomes.csv").open(newline="") as stream:
                outcomes = list(csv.DictReader(stream))
            chosen = next(row for row in outcomes if row["configuration"] == "compact_objective_ir" and row["instance_sha256"] == chosen_sha)
            self.assertEqual(float(chosen["median_runtime_seconds"]), 3)
            self.assertEqual(float(chosen["median_par2_seconds"]), 3)
            with (root / "analysis/benchmark_runtime_summary.csv").open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 27)
            self.assertEqual(report["time_rss_policy"], "optimal-runs")
            self.assertIn("PAR-2 retains all timeouts", (root / "rows.tex").read_text())
            self.assertTrue(json.loads((root / "analysis/conference_consistency_report.json").read_text())["valid"])

    def test_duplicate_execution_is_rejected_even_with_distinct_repetition_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.bundle / "shard-0-of-3/normalized/detailed.csv"
            seed_shard(self.bundle, 0)
            with source.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            rows.append({**rows[0], "repetition": "3", "run_key": "different-label"})
            with self.assertRaisesRegex(ValueError, "duplicate|incomplete"):
                validate_matched_matrix(rows)


if __name__ == "__main__":
    unittest.main()
