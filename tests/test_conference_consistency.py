from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from Conference_Consistency import compare_ir_reference, conference_consistency_report, frozen_stress_bytes, STRESS_COMMIT


class ConferenceConsistencyTests(unittest.TestCase):
    def test_review_export_needs_no_git_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = "output/reference.csv"
            exported = root / "frozen/conference" / relative
            exported.parent.mkdir(parents=True)
            raw = b"status\nOPTIMAL\n"
            exported.write_bytes(raw)
            manifest = root / "journal_configs/conference_consistency_sources.json"
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({"sources": [{
                "path": relative, "git_commit": STRESS_COMMIT,
                "sha256": hashlib.sha256(raw).hexdigest()}]}))
            with patch("Conference_Consistency.PROJECT_ROOT", root), \
                 patch("Conference_Consistency.subprocess.check_output", side_effect=AssertionError("Git unavailable")):
                self.assertEqual(frozen_stress_bytes(relative), raw)
                exported.write_bytes(raw + b"changed")
                with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                    frozen_stress_bytes(relative)

    def test_changed_camera_ready_source_fails_the_hash_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manuscript = root / "paper.tex"
            manuscript.write_text("frozen camera-ready")
            pin = {"path": "manuscript/conference_101719.tex", "location": "workspace",
                   "sha256": hashlib.sha256(manuscript.read_bytes()).hexdigest()}
            config = root / "journal_configs/conference_consistency_sources.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"sources": [pin]}))
            manuscript.write_text("changed camera-ready")
            with patch("Conference_Consistency.PROJECT_ROOT", root), \
                 patch("Conference_Consistency.DEFAULT_IR_CSVS", ()), \
                 patch("Conference_Consistency.STRESS_PATHS", ()), \
                 patch("Conference_Consistency._historical_ir_rows", return_value=({}, {})):
                with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                    conference_consistency_report([], manuscript=manuscript)

    def setUp(self):
        self.reference = [{"instance_sha256": str(i), "status": status,
                           "objective_value": "2" if status == "OPTIMAL" else "", "n_vars": "10"}
                          for i, status in enumerate(("OPTIMAL", "UNSAT"))]
        self.current = []
        for config in ("compact_base_ir", "compact_objective_ir"):
            for rep in (1, 2, 3):
                for original in self.reference:
                    r = dict(original, planned_configuration_id=config, repetition=str(rep),
                             domain_mode="reduced", objective_mode="ir")
                    if config == "compact_objective_ir":
                        r["n_vars"] = "9"
                    self.current.append(r)

    def check(self):
        return compare_ir_reference(self.reference, self.current, expected_instances=2,
                                    formula_fields=("n_vars",))

    def test_solved_includes_optimal_and_unsat_and_all_repetitions_are_checked(self):
        report = self.check()
        self.assertEqual(len(report["checks"]), 6)
        for group in report["checks"]:
            self.assertEqual((group["OPTIMAL"], group["UNSAT"], group["Solved"]), (1, 1, 2))

    def test_missing_and_duplicate_contents_are_rejected(self):
        self.current.pop()
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.check()
        self.current.append(copy.deepcopy(self.current[-1]))
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.check()

    def test_same_count_with_different_content_is_rejected(self):
        self.current[0]["instance_sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.check()

    def test_status_mismatch_is_rejected_even_when_other_runs_match(self):
        self.current[-1]["status"] = "TIMEOUT"
        with self.assertRaisesRegex(ValueError, "status mismatch"):
            self.check()

    def test_optimum_mismatch_is_rejected(self):
        self.current[-2]["objective_value"] = "3"
        with self.assertRaisesRegex(ValueError, "optimum mismatch"):
            self.check()

    def test_base_formula_mismatch_is_rejected(self):
        self.current[0]["n_vars"] = "11"
        with self.assertRaisesRegex(ValueError, "formula mismatch"):
            self.check()

    def test_objective_or_feasible_set_change_is_rejected(self):
        for field, value in (("objective_mode", "bg_d2"), ("domain_mode", "full"),
                             ("participant_idle_cap_rule", "bounded")):
            with self.subTest(field=field):
                original = self.current[0].get(field)
                self.current[0][field] = value
                with self.assertRaisesRegex(ValueError, "feasible-set mismatch"):
                    self.check()
                if original is None:
                    self.current[0].pop(field)
                else:
                    self.current[0][field] = original
