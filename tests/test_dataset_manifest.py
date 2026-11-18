from __future__ import annotations

import csv
import sys
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Dataset_Manifest import base_lineage_id, file_sha256
from Main import (
    collect_instances,
    instance_precedence_configurations,
    parse_args,
)


class DatasetManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = PROJECT_ROOT / "instances_manifest.csv"

    def test_official_manifest_has_expected_distinct_content_counts(self) -> None:
        with self.manifest.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))

        self.assertEqual(len(rows), 126)
        self.assertEqual(
            Counter(row["family"] for row in rows),
            Counter(
                {
                    "original": 20,
                    "forbidden": 26,
                    "fixed": 40,
                    "precedence": 40,
                }
            ),
        )
        self.assertEqual(sum(int(row["source_alias_count"]) for row in rows), 140)
        self.assertEqual(
            sum(int(row["repository_alias_count"]) for row in rows),
            140,
        )
        self.assertEqual(
            sum(int(row["source_alias_count"]) == 2 for row in rows),
            14,
        )
        lineages = {row["base_lineage_id"] for row in rows}
        self.assertEqual(len(lineages), 20)
        self.assertTrue(
            all(lineage.startswith("b2b-lineage-") for lineage in lineages)
        )

    def test_lineage_is_shared_across_official_and_stress_variants(self) -> None:
        manifests = [
            PROJECT_ROOT / "instances_manifest.csv",
            PROJECT_ROOT / "data_precedence_stress" / "instances_manifest.csv",
            PROJECT_ROOT
            / "data_precedence_stress_high"
            / "instances_manifest.csv",
        ]
        lineage_sets = []
        for manifest in manifests:
            with manifest.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            lineage_sets.append({row["base_lineage_id"] for row in rows})
            for row in rows:
                self.assertEqual(
                    row["base_lineage_id"],
                    base_lineage_id(row["canonical_instance"]),
                )

        self.assertEqual(
            [len(lineages) for lineages in lineage_sets],
            [20, 20, 20],
        )
        self.assertEqual(lineage_sets[0], lineage_sets[1])
        self.assertEqual(lineage_sets[0], lineage_sets[2])

    def test_manifest_paths_match_the_recorded_content_hash(self) -> None:
        with self.manifest.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            path = PROJECT_ROOT / row["canonical_run_path"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(file_sha256(path), row["sha256"])

    def test_runner_uses_canonical_contents_and_family_filter(self) -> None:
        instances = collect_instances(
            None,
            None,
            str(self.manifest),
            "forbidden",
        )
        self.assertEqual(len(instances), 26)
        self.assertEqual(len({instance.sha256 for instance in instances}), 26)

    def test_production_defaults_collapse_pg_without_precedence(self) -> None:
        args = parse_args([])
        self.assertEqual(args.encoding_variant, "imp12+")

        original = collect_instances(
            str(PROJECT_ROOT / "data_table03_origin" / "tic-12.original.dzn"),
            None,
        )[0]
        precedence = collect_instances(
            str(PROJECT_ROOT / "data_table08_prec" / "tic-12.prec15.dzn"),
            None,
        )[0]
        self.assertEqual(
            instance_precedence_configurations(args, original),
            [("pairwise", "direct")],
        )
        self.assertEqual(len(instance_precedence_configurations(args, precedence)), 4)


if __name__ == "__main__":
    unittest.main()
