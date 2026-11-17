from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Prepare_Conference_IR_Reruns import (  # noqa: E402
    FORMULA_IDENTITY_FIELDS,
    audit_equivalence,
    index_unique,
    prepare_archive,
)
from Validate_Journal_Run import validate_campaign  # noqa: E402


def identity_row(content_id: str = "content") -> dict[str, str]:
    row = {
        "instance_content_id": content_id,
        "instance_sha256": "a" * 64,
        "status": "OPTIMAL",
        "sat_result": "SAT",
        "primary_objective_value": "2",
        "proven_objective_vector": "2",
        "objective_vector": "2",
        "precedence_relation_edges": "0",
        "precedence_encoding": "pairwise",
        "precedence_graph": "direct",
        "precedence_configuration": "pairwise+direct",
        "factor_p": "Pairwise",
        "factor_g": "Direct-E",
    }
    row.update({field: "1" for field in FORMULA_IDENTITY_FIELDS})
    row["precedence_relation_edges"] = "0"
    return row


class ConferenceIRRerunArchiveTests(unittest.TestCase):
    def test_index_rejects_missing_and_duplicate_rows(self) -> None:
        with self.assertRaises(ValueError):
            index_unique([identity_row()], label="test", expected=2)
        with self.assertRaises(ValueError):
            index_unique(
                [identity_row(), identity_row()], label="test", expected=2
            )

    def test_equivalence_rejects_objective_and_formula_mismatches(self) -> None:
        source = {"content": identity_row()}
        objective_mismatch = identity_row()
        objective_mismatch["objective_vector"] = "3"
        with self.assertRaises(ValueError):
            audit_equivalence(
                source, {"content": objective_mismatch}, label="objective"
            )

        formula_mismatch = identity_row()
        formula_mismatch["n_total_clauses"] = "2"
        with self.assertRaises(ValueError):
            audit_equivalence(
                source, {"content": formula_mismatch}, label="formula"
            )

    def test_empty_precedence_metadata_requires_identical_formula(self) -> None:
        source = identity_row()
        reference = identity_row()
        reference.update(
            {
                "precedence_encoding": "sparse_suffix",
                "precedence_graph": "distance_closure",
                "precedence_configuration": "sparse_suffix+distance_closure",
                "factor_p": "SparseSuffix",
                "factor_g": "DistanceClosure-E*",
            }
        )
        report = audit_equivalence(
            {"content": source}, {"content": reference}, label="inert metadata"
        )
        self.assertEqual(report["accepted_empty_precedence_metadata_differences"], 1)

        reference["precedence_relation_edges"] = "1"
        with self.assertRaises(ValueError):
            audit_equivalence(
                {"content": source}, {"content": reference}, label="active metadata"
            )

    def test_real_archive_contains_452_validated_rows_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "conference-ir-reruns"
            report = prepare_archive(output)
            self.assertTrue(report["valid"])
            errors, validation = validate_campaign(
                output, allow_dirty=True, allow_environment_drift=True
            )
            self.assertEqual(errors, [])
            self.assertEqual(validation["latest_attempts"], 452)

            with (output / "normalized" / "detailed.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 452)
            self.assertEqual(
                {row["repetition"] for row in rows}, {"1", "2"}
            )
            self.assertTrue(
                all(row["archive_source_file_sha256"] for row in rows)
            )
            self.assertTrue(all(row["archive_source_path"] for row in rows))

            runtime = json.loads(
                (output / "paired_runtime_variation_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertLess(runtime["maximum_larger_over_smaller"], 1.052)
            self.assertEqual(runtime["ratios_above_1_5"], 0)


if __name__ == "__main__":
    unittest.main()
