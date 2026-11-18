from __future__ import annotations

import json
import fcntl
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from Split_Matched_Encoding_Shard import split_shard


class SplitMatchedEncodingShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="split-shard-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "parent"
        (self.source / "raw").mkdir(parents=True)
        jobs = []
        for content in range(9):
            for config in range(2):
                key = f"run-{content}-{config}"
                jobs.append({"run_key": key, "instance_content_id": f"content-{content}"})
        (self.source / "plan.json").write_text(json.dumps({"plan_sha256": "plan", "jobs": jobs}))
        records = []
        for job in jobs:
            content = int(job["instance_content_id"].split("-")[-1])
            if content in (0, 3, 6):
                records.append({"run_key": job["run_key"], "attempt": 1,
                                "row": {"run_key": job["run_key"], "status": "OPTIMAL",
                                         "execution_shard_count": 3,
                                         "execution_shard_index": 0,
                                         "campaign_log": "logs/a.log"}})
        (self.source / "raw/results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        (self.source / "logs").mkdir()
        (self.source / "logs/a.log").write_text("log")
        (self.source / "environment.json").write_text(json.dumps({
            "plan_sha256": "plan", "execution_shard_count": 3,
            "execution_shard_indices": [0], "hostname": "original-vm"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_disjoint_refinement_preserves_existing_records_and_reports_pending(self):
        report = split_shard(self.source, self.root / "split", new_indices=(0, 3, 6))
        self.assertEqual(report["source_record_count"], 6)
        self.assertEqual(report["source_pending_count"], 0)
        self.assertEqual([item["expected_jobs"] for item in report["shards"]], [2, 2, 2])
        for item in report["shards"]:
            path = Path(item["path"])
            rows = [json.loads(line) for line in (path / "raw/results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            environment = json.loads((path / "environment.json").read_text())
            self.assertEqual(environment["execution_shard_count"], 9)
            self.assertEqual(environment["execution_shard_indices"], [item["shard_index"]])
            self.assertEqual(environment["hostname"], "original-vm")
            self.assertTrue((path / "provenance/parent_environment.json").is_file())
            self.assertTrue((path / "normalized/detailed.csv").is_file())
            for record in rows:
                self.assertEqual(record["row"]["execution_shard_count"], 9)
                self.assertEqual(record["row"]["execution_shard_index"], item["shard_index"])
                self.assertTrue((path / record["row"]["campaign_log"]).is_file())
                self.assertEqual(len(record["row"]["split_parent_record_sha256"]), 64)

    def test_partial_partition_gets_csv_and_parent_provenance_without_invented_environment(self):
        records = (self.source / "raw/results.jsonl").read_text().splitlines()
        (self.source / "raw/results.jsonl").write_text("\n".join(records[:-1]) + "\n")
        report = split_shard(self.source, self.root / "split")
        self.assertEqual(report["source_pending_count"], 1)
        path = self.root / "split/shard-6-of-9"
        self.assertFalse((path / "environment.json").exists())
        self.assertTrue((path / "normalized/detailed.csv").exists())
        self.assertTrue((path / "provenance/parent_environment.json").exists())

    def test_active_worker_and_incomplete_refinement_are_rejected(self):
        with (self.source / ".worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "worker is still active"):
                split_shard(self.source, self.root / "split")
        for kwargs in ({"new_indices": (0, 3)}, {"new_count": 8, "new_indices": (0, 3, 6)}):
            with self.assertRaises(ValueError):
                split_shard(self.source, self.root / "split", **kwargs)
        self.assertFalse((self.root / "split").exists())

    def test_destination_collision_does_not_create_partial_partition(self):
        collision = self.root / "split/shard-6-of-9"
        collision.mkdir(parents=True)
        (collision / "keep").write_text("existing")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            split_shard(self.source, self.root / "split")
        self.assertFalse((self.root / "split/shard-0-of-9").exists())
        self.assertEqual((collision / "keep").read_text(), "existing")

    def test_nonterminal_source_is_rejected(self):
        records = [json.loads(line) for line in (self.source / "raw/results.jsonl").read_text().splitlines()]
        records[0]["row"]["status"] = "ERROR"
        (self.source / "raw/results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        with self.assertRaisesRegex(ValueError, "nonterminal"):
            split_shard(self.source, self.root / "split", new_indices=(0, 3, 6))

    def test_source_outside_parent_is_rejected(self):
        records = [json.loads(line) for line in (self.source / "raw/results.jsonl").read_text().splitlines()]
        records.append({"run_key": "run-1-0", "attempt": 1,
                        "row": {"run_key": "run-1-0", "status": "OPTIMAL"}})
        (self.source / "raw/results.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        with self.assertRaisesRegex(ValueError, "outside parent"):
            split_shard(self.source, self.root / "split", new_indices=(0, 3, 6))


if __name__ == "__main__":
    unittest.main()
