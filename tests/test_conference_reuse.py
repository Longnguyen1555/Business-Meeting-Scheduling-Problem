from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Audit_Conference_Runtime_Sensitivity import audit_sensitivity
from Prepare_Conference_Reuse import prepare_archive
from Validate_Journal_Run import validate_campaign


class ConferenceReuseTests(unittest.TestCase):
    def test_prepares_complete_formula_identical_historical_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conference_reuse_") as temporary:
            output = Path(temporary) / "archive"
            report = prepare_archive(output)
            self.assertTrue(report["valid"])
            self.assertEqual(report["planned_jobs"], 252)
            errors, _ = validate_campaign(
                output, allow_dirty=True, allow_environment_drift=True
            )
            self.assertEqual(errors, [])
            with (output / "normalized" / "detailed.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 252)
            self.assertEqual(
                {row["archive_formula_identity_verified"] for row in rows},
                {"True"},
            )
            self.assertEqual(
                {row["archive_historical_role"] for row in rows},
                {
                    "bg_d2_independent_repetition_2",
                    "conference_ir_historical_repetition_2",
                },
            )
            equivalence = json.loads(
                (output / "equivalence_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(equivalence["bg_d2"]["identity_mismatches"], 0)
            self.assertEqual(equivalence["ir"]["identity_mismatches"], 0)
            self.assertTrue(
                equivalence["historical_ir_environment_sensitivity"]
                ["runtime_sensitivity_required"]
            )

    def test_runtime_sensitivity_excludes_exactly_one_ir_rep(self) -> None:
        conference = []
        comparison = []
        for content in range(126):
            for repetition, runtime, historical in (
                (1, 10.0, False),
                (2, 8.0, True),
                (3, 12.0, False),
            ):
                conference.append(
                    {
                        "run_key": f"conference-{content}-{repetition}",
                        "instance_content_id": f"content-{content}",
                        "planned_configuration_id": "conference_ir",
                        "objective_mode": "ir",
                        "status": "OPTIMAL",
                        "runtime_seconds": str(runtime),
                        "timeout_seconds": "7200",
                        "peak_memory_mb": "100",
                        "archive_historical_role": (
                            "conference_ir_historical_repetition_2"
                            if historical
                            else ""
                        ),
                    }
                )
            comparison.append(
                {
                    "run_key": f"compact-{content}",
                    "instance_content_id": f"content-{content}",
                    "planned_configuration_id": "compact_cdf_ir",
                    "objective_mode": "ir",
                    "status": "OPTIMAL",
                    "runtime_seconds": "5",
                    "timeout_seconds": "7200",
                    "peak_memory_mb": "80",
                    "archive_historical_role": "",
                }
            )
        report = audit_sensitivity(conference, comparison)
        self.assertEqual(report["historical_rows"], 126)
        self.assertEqual(report["current_environment_rows"], 252)
        self.assertAlmostEqual(
            report["conference_ir_metrics"]["mean_par2_seconds"]
            ["with_historical"],
            10.0,
        )
        self.assertAlmostEqual(
            report["conference_ir_metrics"]["mean_par2_seconds"]
            ["without_historical"],
            11.0,
        )
        self.assertTrue(report["all_rankings_unchanged"])


if __name__ == "__main__":
    unittest.main()
