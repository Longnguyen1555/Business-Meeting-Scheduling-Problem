from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTANCE = PROJECT_ROOT / "data_table03_origin" / "tic-12.original.dzn"


class OrgBaselineTests(unittest.TestCase):
    def test_direct_baseline_builds_every_waiting_objective(self) -> None:
        expected_names = {
            "is": "IdleSum",
            "isq": "SquaredIdleSum",
            "im_is": "MaximumIdleThenIdleSum",
            "ir_is": "IdleRangeThenIdleSum",
            "ir_im_is": "IdleRangeThenMaximumIdleThenIdleSum",
            "ir_im_isq": "IdleRangeThenMaximumIdleThenSquaredIdleSum",
        }
        with tempfile.TemporaryDirectory(prefix="b2b_direct_objectives_") as temp:
            temp_path = Path(temp)
            binary = temp_path / "uwrmaxsat"
            binary.write_text(
                "#!/bin/sh\nprintf 's UNSATISFIABLE\\n'\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)

            for mode, expected_name in expected_names.items():
                with self.subTest(mode=mode):
                    csv_path = temp_path / f"{mode}.csv"
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(PROJECT_ROOT / "src" / "ORG_new.py"),
                            "--instance",
                            str(INSTANCE),
                            "--family",
                            "all",
                            "--objective-mode",
                            mode,
                            "--uwrmaxsat-bin",
                            str(binary),
                            "--timeout",
                            "10",
                            "--csv",
                            str(csv_path),
                            "--excel-dir",
                            str(temp_path / f"excel-{mode}"),
                        ],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    with csv_path.open(newline="", encoding="utf-8") as stream:
                        row = next(csv.DictReader(stream))
                    self.assertEqual(row["factor_o"], expected_name)
                    self.assertEqual(row["model_family"], "DirectPerSlot")
                    self.assertGreater(int(row["n_soft_clauses"]), 0)

    def test_baseline_has_strict_uwr_cli_and_standard_result_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b2b_org_baseline_") as temp:
            temp_path = Path(temp)
            binary = temp_path / "uwrmaxsat"
            binary.write_text(
                "#!/bin/sh\nprintf 's UNSATISFIABLE\\n'\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            csv_path = temp_path / "org.csv"
            excel_dir = temp_path / "excel"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "src" / "ORG_new.py"),
                    "--instance",
                    str(INSTANCE),
                    "--family",
                    "all",
                    "--uwrmaxsat-bin",
                    str(binary),
                    "--timeout",
                    "10",
                    "--csv",
                    str(csv_path),
                    "--excel-dir",
                    str(excel_dir),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["factor_m"], "ORGFull")
            self.assertEqual(rows[0]["factor_o"], "IdleRangePstar")
            self.assertEqual(rows[0]["factor_s"], "UWrMaxSAT")
            self.assertEqual(rows[0]["factor_i"], "OldBestIC12+")
            self.assertEqual(
                rows[0]["configuration_label"],
                "ORG-F-PW-DE-PSC-IRP-UW-OBIC12P",
            )
            self.assertEqual(
                rows[0]["encoding_variant"],
                "org_old_best_ic12plus",
            )
            self.assertEqual(
                rows[0]["implied_constraints_code"],
                "OBIC12P",
            )
            self.assertIn("fairness-none", rows[0]["configuration_id"])
            self.assertIn(
                "i-old_best_ic12plus",
                rows[0]["configuration_id"],
            )
            self.assertEqual(rows[0]["precedence_mode"], "traditional")
            self.assertEqual(
                rows[0]["precedence_configuration"],
                "pairwise+direct",
            )
            self.assertEqual(rows[0]["objective_value"], "")
            self.assertEqual(rows[0]["n_soft_clauses"], rows[0]["soft_weight_sum"])

            workbook = excel_dir / "tic-12.original.xlsx"
            self.assertTrue(workbook.is_file())
            with zipfile.ZipFile(workbook) as archive:
                self.assertIsNone(archive.testzip())

    def test_baseline_timeout_preserves_the_last_uwr_cost(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b2b_org_timeout_") as temp:
            temp_path = Path(temp)
            binary = temp_path / "uwrmaxsat"
            binary.write_text(
                "#!/bin/sh\nprintf 'o 7\\n'\nsleep 2\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            csv_path = temp_path / "org_timeout.csv"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "src" / "ORG_new.py"),
                    "--instance",
                    str(INSTANCE),
                    "--family",
                    "all",
                    "--uwrmaxsat-bin",
                    str(binary),
                    "--timeout",
                    "0.5",
                    "--csv",
                    str(csv_path),
                    "--excel-dir",
                    str(temp_path / "excel"),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with csv_path.open(newline="", encoding="utf-8") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["status"], "TIMEOUT")
            self.assertEqual(row["objective_value"], "7")
            self.assertEqual(row["best_value"], "7")
            self.assertEqual(row["proven_optimum"], "")


if __name__ == "__main__":
    unittest.main()
