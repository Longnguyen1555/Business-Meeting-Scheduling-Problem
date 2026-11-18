from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Generate_Journal_Benchmark import generate_dataset, validate_dataset
from Audit_Journal_Coverage import audit_coverage
from Journal_Instance_Features import extract_instance_features
from Journal_Experiment import build_plan, read_config, resolve_datasets
from Main import collect_instances


class JournalGeneratorAndFeatureTests(unittest.TestCase):
    def test_witness_first_split_is_reproducible_and_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="journal_generator_") as temporary:
            output = Path(temporary) / "generated"
            generate_dataset(
                output,
                n_development=3,
                n_heldout=2,
                master_seed=1234,
            )
            self.assertEqual(validate_dataset(output), [])
            with (output / "generation_manifest.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 5)
            self.assertEqual(
                {row["split"] for row in rows},
                {"development", "heldout"},
            )
            self.assertEqual(len({row["sha256"] for row in rows}), 5)
            self.assertEqual(len({row["base_lineage_id"] for row in rows}), 5)
            self.assertTrue(
                all(row["precedence_density_realized"] for row in rows)
            )

            specs = collect_instances(
                None,
                None,
                str(output / "instances_manifest.csv"),
                "all",
            )
            feature_rows = [
                extract_instance_features(spec, dataset_id="generated")
                for spec in specs
            ]
            features = feature_rows[0]
            self.assertEqual(
                features["instance_content_id"],
                specs[0].content_id,
            )
            self.assertGreater(features["n_meetings"], 0)
            self.assertGreaterEqual(features["domain_removal_ratio"], 0)
            self.assertIn(features["preprocessing_feasible"], {True, False})

            reference = dict(features)
            reference["dataset_id"] = "official"
            report, pair_rows, stratum_rows = audit_coverage(
                [reference, *feature_rows],
                rows,
                expected_development=3,
                expected_heldout=2,
            )
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["generated_rows"], 5)
            self.assertEqual(len(pair_rows), 45)
            self.assertEqual(sum(row["count"] for row in stratum_rows), 5)

            campaign = read_config(
                PROJECT_ROOT / "journal_configs" / "generated_core.json"
            )
            for dataset in campaign["datasets"]:
                manifest_name = (
                    "development_manifest.csv"
                    if "development" in dataset["id"]
                    else "heldout_manifest.csv"
                )
                dataset["manifest"] = str(output / manifest_name)
            plan = build_plan(campaign, resolve_datasets(campaign))
            self.assertEqual(plan["job_count"], 5 * 5 * 3)


if __name__ == "__main__":
    unittest.main()
