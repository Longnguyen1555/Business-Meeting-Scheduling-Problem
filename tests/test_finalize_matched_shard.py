from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from Finalize_Matched_Shard import finalize, wait_for_worker


class FinalizeMatchedShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "shard-0-of-9"
        (self.source / "raw").mkdir(parents=True)
        (self.source / "normalized").mkdir()
        self.config = self.root / "campaign.json"
        self.config.write_text('{"timeout_seconds":7200}')
        self.archive = self.root / "exports/shard-0-of-9.tar.gz"
        (self.source / "environment.json").write_text(json.dumps({
            "plan_sha256": "plan", "execution_shard_count": 9, "execution_shard_indices": [0],
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest()}))
        (self.source / "plan.json").write_text(json.dumps({"plan_sha256": "plan", "jobs": [
            {"instance_content_id": "content", "run_key": "key"}]}))
        (self.source / "campaign_status.json").write_text('{"error_rows":0}')
        (self.source / "raw/results.jsonl").write_text(json.dumps({"run_key": "key", "attempt": 1,
            "row": {"run_key": "key", "status": "TIMEOUT"}}) + "\n")
        (self.source / "normalized/detailed.csv").write_text('run_key,status\nkey,TIMEOUT\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_completed_timeout_is_exported_with_verified_config_and_kept_source(self):
        original = (self.source / "raw/results.jsonl").read_bytes()
        report = finalize(self.source, self.archive, 0, 9, self.config)
        self.assertTrue(report["complete"])
        self.assertEqual(report["outcomes"], {"TIMEOUT": 1})
        self.assertEqual((self.source / "provenance/campaign.json").read_bytes(), self.config.read_bytes())
        self.assertEqual((self.source / "raw/results.jsonl").read_bytes(), original)

    def test_interrupted_or_error_worker_is_not_published(self):
        for status in ({"interrupted": True, "error_rows": 0}, {"error_rows": 1}):
            (self.source / "campaign_status.json").write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError, "interrupted or has ERROR"):
                finalize(self.source, self.archive, 0, 9, self.config)
        self.assertFalse(self.archive.exists())

    def test_wrong_config_and_incomplete_result_are_rejected(self):
        data = self.config.read_bytes()
        self.config.write_bytes(data + b"\n")
        with self.assertRaisesRegex(ValueError, "recorded SHA-256"):
            finalize(self.source, self.archive, 0, 9, self.config)
        self.config.write_bytes(data)
        (self.source / "raw/results.jsonl").write_text("")
        with self.assertRaisesRegex(ValueError, "1 missing"):
            finalize(self.source, self.archive, 0, 9, self.config)
        self.assertFalse(self.archive.exists())

    def test_unrelated_process_and_invalid_pid_are_rejected(self):
        with self.assertRaises(ValueError):
            wait_for_worker(0, self.source)
        if Path("/proc/1/cmdline").exists():
            with self.assertRaisesRegex(ValueError, "different process"):
                wait_for_worker(1, self.source)


if __name__ == "__main__":
    unittest.main()
