from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Dataset_Manifest import classify_instance_name
from B2B_Instance import read_instance
from Generate_Precedence_Stress import validate_dataset


class PrecedenceStressDatasetTests(unittest.TestCase):
    @staticmethod
    def _direct_edges(path: Path) -> set[tuple[int, int]]:
        instance = read_instance(path)
        return {
            (predecessor, successor)
            for successor, predecessors in enumerate(instance.precedences)
            for predecessor in predecessors
        }

    @staticmethod
    def _witness_rows(path: Path) -> dict[str, dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["source_instance"]: row
                for row in csv.DictReader(handle)
            }

    def test_generated_dataset_passes_all_structural_checks(self) -> None:
        summary = validate_dataset(
            PROJECT_ROOT / "data_precedence_stress",
            PROJECT_ROOT / "data_table03_origin",
        )
        self.assertEqual(
            summary,
            {
                "source_instances": 20,
                "gamma_levels": 3,
                "generated_instances": 60,
            },
        )

    def test_high_density_dataset_passes_all_structural_checks(self) -> None:
        summary = validate_dataset(
            PROJECT_ROOT / "data_precedence_stress_high",
            PROJECT_ROOT / "data_table03_origin",
        )
        self.assertEqual(
            summary,
            {
                "source_instances": 20,
                "gamma_levels": 2,
                "generated_instances": 40,
            },
        )

    def test_high_density_dataset_reuses_canonical_witnesses(self) -> None:
        base_rows = self._witness_rows(
            PROJECT_ROOT / "data_precedence_stress" / "witnesses.csv"
        )
        high_rows = self._witness_rows(
            PROJECT_ROOT / "data_precedence_stress_high" / "witnesses.csv"
        )
        self.assertEqual(high_rows, base_rows)

    def test_high_density_edges_extend_prec40_ladders(self) -> None:
        base_directory = PROJECT_ROOT / "data_precedence_stress"
        high_directory = PROJECT_ROOT / "data_precedence_stress_high"
        prec40_paths = sorted(base_directory.glob("*.prec40.dzn"))
        self.assertEqual(len(prec40_paths), 20)
        for prec40_path in prec40_paths:
            stem = prec40_path.name.removesuffix(".prec40.dzn")
            edges40 = self._direct_edges(prec40_path)
            edges50 = self._direct_edges(high_directory / f"{stem}.prec50.dzn")
            edges60 = self._direct_edges(high_directory / f"{stem}.prec60.dzn")
            self.assertLess(edges40, edges50, stem)
            self.assertLess(edges50, edges60, stem)

    def test_generated_density_suffixes_are_precedence_variants(self) -> None:
        for gamma in (30, 35, 40, 50, 60):
            self.assertEqual(
                classify_instance_name(f"tic-12.prec{gamma}.dzn"),
                ("precedence", f"prec{gamma}"),
            )


if __name__ == "__main__":
    unittest.main()
