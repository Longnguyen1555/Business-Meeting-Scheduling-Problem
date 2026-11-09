from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from Merge_Journal_Shards import _solver_pin_only_config_difference


class MatchedMergeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="merge-config-evidence-")
        self.root = Path(self.tmp.name)
        self.plan = {"reuse_policy": "validated_common_gcp"}

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, name, pin, **kwargs):
        path = self.root / name
        (path / "provenance").mkdir(parents=True)
        config = {"timeout_seconds": 7200, "uwrmaxsat_sha256": pin, "blocks": ["same"], **kwargs}
        data = json.dumps(config, indent=2).encode()
        (path / "provenance/campaign.json").write_bytes(data)
        return path, {"uwrmaxsat_sha256": pin, "config_sha256": hashlib.sha256(data).hexdigest()}

    def accepts(self, first, second, **kwargs):
        return _solver_pin_only_config_difference(*first, *second, plan=self.plan,
                                                  allow_mixed_environments=kwargs.get("allow", True))

    def test_only_local_solver_pin_may_differ_with_verified_configuration(self):
        a = self.config("a", "a" * 64)
        b = self.config("b", "b" * 64)
        self.assertTrue(self.accepts(a, b))
        self.assertFalse(self.accepts(a, b, allow=False))
        self.plan["reuse_policy"] = "strict"
        self.assertFalse(self.accepts(a, b))

    def test_timeout_and_objective_changes_are_not_accepted(self):
        a = self.config("a", "a" * 64)
        self.assertFalse(self.accepts(a, self.config("b", "b" * 64, timeout_seconds=3600)))
        self.assertFalse(self.accepts(a, self.config("c", "b" * 64, blocks=["different objective"])))

    def test_missing_or_modified_evidence_is_rejected(self):
        a, b = self.config("a", "a" * 64), self.config("b", "b" * 64)
        file = b[0] / "provenance/campaign.json"
        data = file.read_bytes()
        file.unlink()
        with self.assertRaisesRegex(ValueError, "missing configuration evidence"):
            self.accepts(a, b)
        file.write_bytes(data + b"\n")
        with self.assertRaisesRegex(ValueError, "does not match recorded"):
            self.accepts(a, b)

    def test_configured_pin_must_match_recorded_environment(self):
        a, b = self.config("a", "a" * 64), self.config("b", "b" * 64)
        b[1]["uwrmaxsat_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "solver pin differs"):
            self.accepts(a, b)


if __name__ == "__main__":
    unittest.main()
