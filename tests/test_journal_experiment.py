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
    execution_shard_assignments,
    machine_profile_errors,
    read_config,
    resolve_datasets,
)
from Merge_Journal_Shards import merge_shards
from Select_Final_Tier_Experiment import build_final_tier_config
from Validate_Journal_Run import validate_campaign


class JournalExperimentTests(unittest.TestCase):
    def test_balanced_objective_campaigns_and_final_tier_selection(self) -> None:
        expected = {
            "compact_objectives.json": 1890,
            "compact_objectives_smoke.json": 30,
            "idle_cap_sensitivity.json": 378,
            "idle_cap_sensitivity_smoke.json": 6,
            "main_objectives.json": 2268,
            "main_objectives_smoke.json": 12,
        }
        plans = {}
        for name, count in expected.items():
            config = read_config(PROJECT_ROOT / "journal_configs" / name)
            plan = build_plan(config, resolve_datasets(config))
            self.assertEqual(plan["job_count"], count)
            plans[name] = (config, plan)

        main_config, main_plan = plans["main_objectives.json"]
        self.assertEqual(len(main_plan["reuse_results"]), 3)
        reused_target_cells = {
            (
                rule["target_block_id"],
                rule["target_configuration_id"],
                rule["target_repetition"],
            )
            for rule in main_plan["reuse_results"]
        }
        reused_main_jobs = sum(
            (
                job["experiment_block"],
                job["planned_configuration_id"],
                job["repetition"],
            )
            in reused_target_cells
            for job in main_plan["jobs"]
        )
        self.assertEqual(reused_main_jobs, 378)
        self.assertEqual(main_plan["job_count"] - reused_main_jobs, 1890)
        main_ir_im_is_caps = {
            job["planned_configuration_id"]: job["configuration"].get(
                "participant_idle_cap_rule", "none"
            )
            for job in main_plan["jobs"]
            if job["configuration"].get("objective_mode") == "ir_im_is"
        }
        self.assertEqual(
            main_ir_im_is_caps,
            {
                "published_2022_ir_im_is": "none",
                "compact_cdf_ir_im_is": "none",
            },
        )
        compact_ir_im_is_caps = {
            job["planned_configuration_id"]: job["configuration"].get(
                "participant_idle_cap_rule", "none"
            )
            for job in plans["compact_objectives.json"][1]["jobs"]
            if job["configuration"].get("objective_mode") == "ir_im_is"
        }
        self.assertEqual(set(compact_ir_im_is_caps.values()), {"none"})
        deferred_cap_cells = {
            (
                job["configuration"].get("participant_idle_cap_rule"),
                job["configuration"].get("participant_idle_cap_alpha"),
            )
            for job in plans["idle_cap_sensitivity.json"][1]["jobs"]
        }
        self.assertEqual(
            deferred_cap_cells,
            {
                ("interpolated_bounds", "1/2"),
                ("interpolated_bounds", "3/4"),
                ("interpolated_bounds", "1"),
            },
        )
        rows = [
            {
                "objective_mode": "ir_im_is",
                "planned_configuration_id": "published_2022_ir_im_is",
                "status": "TIMEOUT",
                "runtime_seconds": "7200",
                "peak_memory_mb": "100",
            },
            {
                "objective_mode": "ir_im_is",
                "planned_configuration_id": "compact_cdf_ir_im_is",
                "status": "OPTIMAL",
                "runtime_seconds": "10",
                "peak_memory_mb": "120",
            },
        ]
        final_config, report = build_final_tier_config(
            main_config, main_plan, rows
        )
        self.assertEqual(
            report["selected_configuration_id"],
            "compact_cdf_ir_im_is",
        )
        self.assertEqual(final_config["expected_job_count"], 378)
        final_cells = final_config["blocks"][0]["configurations"]
        self.assertEqual(
            {cell["objective_mode"] for cell in final_cells},
            {"ir_im_isq"},
        )
        self.assertTrue(
            final_config["selection_provenance"]["baseline_results_reused"]
        )
        self.assertTrue(
            all(
                cell.get("participant_idle_cap_rule", "none") == "none"
                for cell in final_cells
            )
        )
        final_plan = build_plan(
            final_config, resolve_datasets(final_config)
        )
        self.assertEqual(final_plan["job_count"], 378)

    def test_all126_content_shards_are_balanced_and_pair_preserving(self) -> None:
        config = read_config(
            PROJECT_ROOT / "journal_configs" / "all126_first.json"
        )
        plan = build_plan(config, resolve_datasets(config))
        assignments = execution_shard_assignments(plan, 4)
        counts = {shard: 0 for shard in range(4)}
        contents: dict[str, set[int]] = {}
        configurations: dict[tuple[int, str], int] = {}
        for job in plan["jobs"]:
            shard = assignments[job["run_key"]]
            counts[shard] += 1
            contents.setdefault(job["instance_content_id"], set()).add(shard)
            key = (shard, job["planned_configuration_id"])
            configurations[key] = configurations.get(key, 0) + 1
        self.assertEqual(counts, {0: 416, 1: 416, 2: 403, 3: 403})
        self.assertTrue(all(len(shards) == 1 for shards in contents.values()))
        for shard, expected in ((0, 32), (1, 32), (2, 31), (3, 31)):
            self.assertEqual(
                {
                    count
                    for (observed_shard, _), count in configurations.items()
                    if observed_shard == shard
                },
                {expected},
            )

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

    def test_two_disjoint_shards_merge_into_one_valid_campaign(self) -> None:
        with tempfile.TemporaryDirectory(prefix="journal_shards_") as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config = {
                "schema_version": 1,
                "campaign_name": "unit-two-shards",
                "timeout_seconds": 30,
                "controller_grace_seconds": 5,
                "run_order_seed": 11,
                "require_clean_worktree": False,
                "datasets": [
                    {
                        "id": "two",
                        "manifest": str(PROJECT_ROOT / "instances_manifest.csv"),
                        "family": "original",
                        "instance_names": [
                            "forum-13.original",
                            "tic-12.original",
                        ],
                    }
                ],
                "blocks": [
                    {
                        "id": "one_block",
                        "datasets": ["two"],
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
            shard_outputs = [root / "shard-0", root / "shard-1"]
            for shard_index, output in enumerate(shard_outputs):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(PROJECT_ROOT / "src" / "Journal_Experiment.py"),
                        "--config",
                        str(config_path),
                        "--output-dir",
                        str(output),
                        "--allow-dirty",
                        "--shard-count",
                        "2",
                        "--shard-index",
                        str(shard_index),
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
            merged = root / "merged"
            report = merge_shards(
                shard_outputs,
                merged,
                allow_dirty=True,
            )
            self.assertTrue(report["valid"])
            self.assertEqual(report["latest_attempts"], 2)
            errors, _ = validate_campaign(
                merged,
                allow_dirty=True,
            )
            self.assertEqual(errors, [])

    def test_reuses_equivalent_result_without_executing_target_cell(self) -> None:
        with tempfile.TemporaryDirectory(prefix="journal_reuse_") as temporary:
            root = Path(temporary)
            source_config_path = root / "source.json"
            target_config_path = root / "target.json"
            common_configuration = {
                "executor": "org_bg_d2",
                "objective_mode": "bg_d2",
                "backend": "rc2",
            }
            common = {
                "schema_version": 1,
                "timeout_seconds": 30,
                "controller_grace_seconds": 5,
                "run_order_seed": 19,
                "require_clean_worktree": False,
                "datasets": [
                    {
                        "id": "one",
                        "manifest": str(PROJECT_ROOT / "instances_manifest.csv"),
                        "family": "original",
                        "instance_names": [
                            "forum-13.original",
                            "tic-12.original",
                        ],
                    }
                ],
            }
            source_config = {
                **common,
                "campaign_name": "unit-reuse-source",
                "blocks": [
                    {
                        "id": "source-block",
                        "datasets": ["one"],
                        "repetitions": 1,
                        "configurations": [
                            {**common_configuration, "id": "source-model"}
                        ],
                    }
                ],
            }
            target_config = {
                **common,
                "campaign_name": "unit-reuse-target",
                "reuse_results": [
                    {
                        "source_campaign_id": "unit-reuse-source",
                        "source_block_id": "source-block",
                        "source_configuration_id": "source-model",
                        "source_repetition": 1,
                        "target_block_id": "target-block",
                        "target_configuration_id": "target-model",
                        "target_repetition": 1,
                    }
                ],
                "blocks": [
                    {
                        "id": "target-block",
                        "datasets": ["one"],
                        "repetitions": 1,
                        "configurations": [
                            {**common_configuration, "id": "target-model"}
                        ],
                    }
                ],
            }
            source_config_path.write_text(
                json.dumps(source_config), encoding="utf-8"
            )
            target_config_path.write_text(
                json.dumps(target_config), encoding="utf-8"
            )
            runner = PROJECT_ROOT / "src" / "Journal_Experiment.py"
            source_outputs = [root / f"source-shard-{index}" for index in range(2)]
            target_outputs = [root / f"target-shard-{index}" for index in range(2)]
            for shard_index, (source_output, target_output) in enumerate(
                zip(source_outputs, target_outputs)
            ):
                shard_args = [
                    "--shard-count",
                    "2",
                    "--shard-index",
                    str(shard_index),
                ]
                source_run = subprocess.run(
                    [
                        sys.executable,
                        str(runner),
                        "--config",
                        str(source_config_path),
                        "--output-dir",
                        str(source_output),
                        "--allow-dirty",
                        *shard_args,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    source_run.returncode,
                    0,
                    source_run.stdout + source_run.stderr,
                )
                target_run = subprocess.run(
                    [
                        sys.executable,
                        str(runner),
                        "--config",
                        str(target_config_path),
                        "--output-dir",
                        str(target_output),
                        "--reuse-output",
                        str(source_output),
                        "--allow-dirty",
                        *shard_args,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    target_run.returncode,
                    0,
                    target_run.stdout + target_run.stderr,
                )
                self.assertIn("imported=1", target_run.stdout)
                self.assertIn("pending_selected=0", target_run.stdout)
                target_resume = subprocess.run(
                    [
                        sys.executable,
                        str(runner),
                        "--config",
                        str(target_config_path),
                        "--output-dir",
                        str(target_output),
                        "--reuse-output",
                        str(source_output),
                        "--allow-dirty",
                        "--resume",
                        *shard_args,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    target_resume.returncode,
                    0,
                    target_resume.stdout + target_resume.stderr,
                )
                self.assertIn("already_completed=1", target_resume.stdout)
                self.assertIn("pending_selected=0", target_resume.stdout)
                records = [
                    json.loads(line)
                    for line in (
                        target_output / "raw" / "results.jsonl"
                    ).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(len(records), 1)
                row = records[0]["row"]
                self.assertEqual(row["result_origin"], "reused")
                self.assertEqual(
                    row["reused_from_configuration_id"], "source-model"
                )
                self.assertEqual(
                    row["planned_configuration_id"], "target-model"
                )
                errors, report = validate_campaign(
                    target_output,
                    allow_dirty=True,
                    allow_incomplete=True,
                )
                self.assertEqual(errors, [])
                self.assertTrue(report["valid"])

            merged = root / "target-merged"
            merge_report = merge_shards(
                target_outputs,
                merged,
                allow_dirty=True,
            )
            self.assertEqual(merge_report["latest_attempts"], 2)
            errors, report = validate_campaign(
                merged,
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
