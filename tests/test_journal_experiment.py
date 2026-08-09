from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Journal_Experiment import (
    build_plan,
    machine_profile_errors,
    read_config,
    resolve_datasets,
)
from Validate_Journal_Run import validate_campaign


class JournalExperimentTests(unittest.TestCase):
    def test_frozen_machine_profile_comparison(self) -> None:
        required = {
            "cpu_model_contains": "Xeon(R) Platinum 8581C",
            "physical_cpu_cores": 4,
            "logical_cpu_cores": 8,
            "system_memory_mb_min": 15000,
            "system_memory_mb_max": 16500,
            "swap_memory_mb_max": 0,
        }
        matching = {
            "cpu_model": "Intel(R) XEON(R) PLATINUM 8581C CPU @ 2.30GHz",
            "physical_cpu_cores": 4,
            "logical_cpu_cores": 8,
            "system_memory_mb": 15988.062,
            "swap_memory_mb": 0,
        }
        self.assertEqual(machine_profile_errors(required, matching), [])
        mismatching = dict(matching, logical_cpu_cores=4, swap_memory_mb=512)
        errors = machine_profile_errors(required, mismatching)
        self.assertTrue(any("logical_cpu_cores" in error for error in errors))
        self.assertTrue(any("swap_memory_mb" in error for error in errors))

    def test_frozen_production_plan_cell_counts(self) -> None:
        expected = {
            "correctness.json": 108,
            "official_core.json": 8820,
            "precedence_ablation.json": 3360,
            "production_smoke.json": 168,
            "warmup.json": 3,
        }
        for name, count in expected.items():
            path = PROJECT_ROOT / "journal_configs" / name
            config = read_config(path)
            first = build_plan(config, resolve_datasets(config))
            second = build_plan(config, resolve_datasets(config))
            self.assertEqual(first["job_count"], count)
            self.assertEqual(first["plan_sha256"], second["plan_sha256"])
            self.assertEqual(
                len({job["run_key"] for job in first["jobs"]}),
                count,
            )

    def test_production_resources_are_frozen_to_conference_protocol(self) -> None:
        production = (
            "official_core.json",
            "precedence_ablation.json",
            "generated_core.json",
            "pilot.json",
        )
        for name in production:
            config = read_config(PROJECT_ROOT / "journal_configs" / name)
            self.assertEqual(config["timeout_seconds"], 7200)
            required = config["required_machine"]
            self.assertEqual(required["physical_cpu_cores"], 4)
            self.assertEqual(required["logical_cpu_cores"], 8)
            self.assertEqual(required["threads_per_run"], 1)
            self.assertEqual(required["random_seed"], 0)
            self.assertEqual(required["max_peak_memory_fraction"], 0.8)

    def test_append_only_cell_runner_resumes_without_duplicate_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="journal_runner_") as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            output = root / "campaign"
            config = {
                "schema_version": 1,
                "campaign_name": "unit-resume",
                "timeout_seconds": 30,
                "controller_grace_seconds": 5,
                "run_order_seed": 7,
                "require_clean_worktree": False,
                "datasets": [
                    {
                        "id": "one",
                        "manifest": str(PROJECT_ROOT / "instances_manifest.csv"),
                        "family": "original",
                        "instance_names": ["tic-12.original"],
                    }
                ],
                "blocks": [
                    {
                        "id": "one_block",
                        "datasets": ["one"],
                        "repetitions": 1,
                        "configurations": [
                            {
                                "id": "org_bg_rc2",
                                "executor": "org_bg_d2",
                                "objective_mode": "bg_d2",
                                "backend": "rc2",
                            }
                        ],
                    }
                ],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            base_command = [
                sys.executable,
                str(PROJECT_ROOT / "src" / "Journal_Experiment.py"),
                "--config",
                str(config_path),
                "--output-dir",
                str(output),
                "--allow-dirty",
            ]
            first = subprocess.run(
                base_command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = subprocess.run(
                [*base_command, "--resume"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            raw_lines = (output / "raw" / "results.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(raw_lines), 1)
            with (output / "normalized" / "detailed.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            errors, report = validate_campaign(
                output,
                allow_dirty=True,
            )
            self.assertEqual(errors, [])
            self.assertTrue(report["valid"])

    def test_sigterm_leaves_active_cell_uncommitted_for_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="journal_signal_") as temporary:
            root = Path(temporary)
            binary = root / "uwrmaxsat"
            binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            binary.chmod(0o755)
            binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
            config_path = root / "config.json"
            output = root / "campaign"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "campaign_name": "unit-signal",
                        "timeout_seconds": 30,
                        "controller_grace_seconds": 5,
                        "require_clean_worktree": False,
                        "datasets": [
                            {
                                "id": "one",
                                "manifest": str(
                                    PROJECT_ROOT / "instances_manifest.csv"
                                ),
                                "family": "original",
                                "instance_names": ["tic-12.original"],
                            }
                        ],
                        "blocks": [
                            {
                                "id": "signal_block",
                                "datasets": ["one"],
                                "repetitions": 1,
                                "configurations": [
                                    {
                                        "id": "sleeping_uwr",
                                        "executor": "main",
                                        "solver": "maxsat",
                                        "objective_mode": "ir",
                                        "domain_mode": "reduced",
                                        "domain_filter_graph": "distance_closure",
                                        "precedence_encoding": "sparse_suffix",
                                        "precedence_graph": "distance_closure",
                                        "encoding_variant": "imp12+",
                                        "maxsat_backend": "uwrmaxsat",
                                        "sat_backend": "cadical",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "src" / "Journal_Experiment.py"),
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(output),
                    "--uwrmaxsat-bin",
                    str(binary),
                    "--uwrmaxsat-sha256",
                    binary_hash,
                    "--allow-dirty",
                ],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not list(
                (output / "logs").glob("*.log")
            ):
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIsNone(process.poll())
            time.sleep(0.2)
            process.terminate()
            stdout, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stdout)
            raw = output / "raw" / "results.jsonl"
            self.assertFalse(raw.exists() and raw.read_text().strip())
            status = json.loads(
                (output / "campaign_status.json").read_text(encoding="utf-8")
            )
            self.assertTrue(status["interrupted"])
            self.assertEqual(status["latest_rows"], 0)


if __name__ == "__main__":
    unittest.main()
