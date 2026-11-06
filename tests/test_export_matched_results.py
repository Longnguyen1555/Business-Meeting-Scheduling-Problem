from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from Export_Matched_Results import export_shard


class MatchedResultExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="matched-export-")
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "runtime/shard-0-of-3"
        self.archive = self.directory / "checkout/results/shard-0-of-3.tar.gz"
        (self.source / "raw/logs").mkdir(parents=True)
        (self.source / "normalized").mkdir()
        jobs = [{"instance_content_id": f"content-{index}", "run_key": f"run-{index}"}
                for index in range(6)]
        (self.source / "plan.json").write_text(json.dumps({"plan_sha256": "plan-hash", "jobs": jobs}))
        environment = {"plan_sha256": "plan-hash", "execution_shard_count": 3,
                       "execution_shard_indices": [0], "uwrmaxsat_sha256": "recorded-local-pin"}
        (self.source / "environment.json").write_text(json.dumps(environment))
        records = [self.record("run-0", "OPTIMAL"), self.record("run-3", "TIMEOUT")]
        self.write_records(records)
        (self.source / "normalized/detailed.csv").write_text("run_key,status\nrun-0,OPTIMAL\nrun-3,TIMEOUT\n")
        (self.source / "raw/logs/solver.log").write_bytes(b"original solver output\n")
        (self.source / ".worker.lock").touch()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def record(key, status, attempt=1):
        return {"run_key": key, "attempt": attempt, "row": {"run_key": key, "status": status}}

    def write_records(self, records):
        (self.source / "raw/results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))

    def test_exports_all_file_bytes_and_hashes_with_logs_and_timeout(self):
        before = {path.relative_to(self.source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.source.rglob("*") if path.is_file()}
        report = export_shard(self.source, self.archive, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(report["outcomes"], {"OPTIMAL": 1, "TIMEOUT": 1})
        with tarfile.open(self.archive) as stream:
            names = set(stream.getnames())
            self.assertNotIn("shard-0-of-3/.worker.lock", names)
            self.assertIn("shard-0-of-3/raw/logs/solver.log", names)
            manifest = json.load(stream.extractfile("shard-0-of-3/export_manifest.json"))
            self.assertEqual(manifest["inventory"]["observed_cells"], 2)
            for name, (data, modified) in before.items():
                self.assertEqual((self.source / name).read_bytes(), data)
                self.assertEqual((self.source / name).stat().st_mtime_ns, modified)
                if name != ".worker.lock":
                    self.assertEqual(stream.extractfile("shard-0-of-3/" + name).read(), data)
                    self.assertIn(name, manifest["files"])

    def test_repeat_export_is_idempotent_and_changed_source_is_not_overwritten(self):
        export_shard(self.source, self.archive, 0)
        original = self.archive.read_bytes()
        modified = self.archive.stat().st_mtime_ns
        export_shard(self.source, self.archive, 0)
        self.assertEqual(self.archive.stat().st_mtime_ns, modified)
        (self.source / "raw/logs/solver.log").write_text("updated\n")
        with self.assertRaisesRegex(ValueError, "differs from source"):
            export_shard(self.source, self.archive, 0)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_active_worker_prevents_export(self):
        with (self.source / ".worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "worker is still active"):
                export_shard(self.source, self.archive, 0)
        self.assertFalse(self.archive.exists())

    def test_incomplete_or_error_results_require_diagnostic_flag(self):
        self.write_records([self.record("run-0", "ERROR")])
        with self.assertRaisesRegex(ValueError, "1 missing and 1 nonterminal"):
            export_shard(self.source, self.archive, 0)
        report = export_shard(self.source, self.archive, 0, allow_incomplete=True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["outcomes"], {"ERROR": 1})
        self.assertEqual(report["missing_cells"], 1)

    def test_wrong_index_or_unassigned_records_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not identify shard 1"):
            export_shard(self.source, self.archive, 1)
        self.write_records([self.record("run-1", "OPTIMAL")])
        with self.assertRaisesRegex(ValueError, "unassigned run keys"):
            export_shard(self.source, self.archive, 0)

    def test_retried_error_and_terminal_result_preserve_every_attempt(self):
        self.write_records([self.record("run-0", "ERROR"), self.record("run-0", "OPTIMAL", 2),
                            self.record("run-3", "UNSAT")])
        report = export_shard(self.source, self.archive, 0)
        self.assertEqual(report["raw_attempts"], 3)
        self.assertEqual(report["observed_cells"], 2)
        self.assertEqual(report["outcomes"], {"OPTIMAL": 1, "UNSAT": 1})

    def test_invalid_jsonl_and_duplicate_attempt_are_rejected(self):
        (self.source / "raw/results.jsonl").write_text('{"run_key":')
        with self.assertRaisesRegex(ValueError, "invalid record at JSONL line 1"):
            export_shard(self.source, self.archive, 0)
        self.write_records([self.record("run-0", "OPTIMAL"), self.record("run-0", "OPTIMAL")])
        with self.assertRaisesRegex(ValueError, "attempt numbers are not increasing"):
            export_shard(self.source, self.archive, 0)

    def test_symlink_and_archive_inside_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source"):
            export_shard(self.source, self.source / "result.tar.gz", 0)
        (self.source / "linked-log").symlink_to(self.source / "raw/logs/solver.log")
        with self.assertRaisesRegex(ValueError, "symlink"):
            export_shard(self.source, self.archive, 0)

    def test_missing_source_never_creates_runtime(self):
        absent = self.directory / "absent-runtime/shard-0-of-3"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            export_shard(absent, self.archive, 0)
        self.assertFalse(absent.parent.exists())

    def test_launcher_exports_without_importing_bootstrap_or_loading_solver_environment(self):
        project = self.directory / "clean-checkout"
        (project / "src").mkdir(parents=True)
        (project / "scripts").mkdir()
        for name in ("src/Export_Matched_Results.py", "scripts/run_matched_encoding_three_vms.sh"):
            shutil.copyfile(ROOT / name, project / name)
        # These would fail if exporting dispatched through any runtime preparation.
        (project / "src/Bootstrap_Matched_Evaluation.py").write_text("raise RuntimeError('must not bootstrap')\n")
        (project / "journal_gcp.env").write_text("exit 99\n")
        result = subprocess.run(["bash", "scripts/run_matched_encoding_three_vms.sh", "export", "0",
                                 "--source", str(self.source)], cwd=project,
                                env={**os.environ, "JOURNAL_PYTHON": "/missing/python"},
                                capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        archive = project / "outputs/journal/matched-encoding-v2-results/shard-0-of-3.tar.gz"
        self.assertEqual(Path(report["archive"]), archive.resolve())
        self.assertTrue(archive.exists())
        self.assertIn("git add -- outputs/journal/matched-encoding-v2-results/shard-0-of-3.tar.gz", result.stderr)
        self.assertFalse((project.parent / "b2b-matched-v2").exists())


if __name__ == "__main__":
    unittest.main()
