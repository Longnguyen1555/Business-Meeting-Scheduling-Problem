"""Distribution tests: only committed files and the published archive may be used."""
from __future__ import annotations

import io
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
import Bootstrap_Matched_Evaluation as bootstrap


class PublishedCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="matched-distribution-")
        cls.directory = Path(cls.temporary.name)
        cls.checkout = cls.directory / "checkout"
        cls.runtime = cls.directory / "frozen runtime"
        # A clone does not contain the author's other uncommitted dependencies.
        subprocess.run(["git", "clone", "--quiet", "--shared", str(ROOT),
                        str(cls.checkout)], check=True, capture_output=True)
        for name in ("scripts/run_matched_encoding_three_vms.sh",
                     "src/Bootstrap_Matched_Evaluation.py",
                     "src/Matched_Encoding_Evaluation.py"):
            shutil.copyfile(ROOT / name, cls.checkout / name)
        cls.env = {key: value for key, value in os.environ.items()
                   if key not in ("JOURNAL_ENV_FILE", "JOURNAL_PYTHON", "PLAN_PYTHON",
                                  "MATCHED_OUTPUT", "MATCHED_MERGED_OUTPUT",
                                  "MATCHED_RUNTIME_DIR", "UWRMAXSAT_BIN",
                                  "UWRMAXSAT_SHA256", "CPU_CORE")}
        cls.env.update(MATCHED_RUNTIME_DIR=str(cls.runtime),
                       PLAN_PYTHON=sys.executable, JOURNAL_PYTHON=sys.executable)
        result = cls.launch("plan")
        cls.report = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def launch(cls, *args, env=None, check=True):
        return subprocess.run(["bash", "scripts/run_matched_encoding_three_vms.sh", *args],
                              cwd=cls.checkout, env=env or cls.env,
                              text=True, capture_output=True, check=check)

    def test_fresh_clone_installs_complete_frozen_campaign(self):
        self.assertEqual(self.report["planned_jobs"], 6102)
        self.assertEqual(self.report["reused_jobs"], 4960)
        self.assertEqual(self.report["remaining_jobs"], 1142)
        self.assertEqual([self.report["shards"][str(i)]["remaining_jobs"]
                          for i in range(3)], [382, 379, 381])
        campaign = json.loads((self.runtime / bootstrap.BUNDLE / "campaign.json").read_text())
        for name in campaign["frozen_sources"]["code_files"]:
            self.assertTrue((self.runtime / name).is_file(), name)
        # No extra bootstrap module may change the frozen worker's source hash.
        self.assertFalse((self.runtime / "src/Bootstrap_Matched_Evaluation.py").exists())

    def test_repeated_plan_preserves_existing_results(self):
        marker = self.runtime / bootstrap.BUNDLE / "shard-0-of-3/raw/sentinel.jsonl"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("existing execution record\n")
        before = marker.stat().st_mtime_ns
        result = self.launch("plan")
        self.assertEqual(json.loads(result.stdout), self.report)
        self.assertEqual(marker.stat().st_mtime_ns, before)
        self.assertEqual(marker.read_text(), "existing execution record\n")

    def test_published_python_entry_point_works_without_local_dependencies(self):
        result = subprocess.run([sys.executable, "-B", "src/Matched_Encoding_Evaluation.py",
                                 "plan", "--plan-only"], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), self.report)

    def test_all_three_shards_receive_correct_worker_arguments(self):
        # Capture the dispatched argv instead of starting any solver.
        executable = self.directory / "capture python"
        executable.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        executable.chmod(0o755)
        env = dict(self.env, JOURNAL_PYTHON=str(executable),
                   UWRMAXSAT_BIN="bin/uwrmaxsat", UWRMAXSAT_SHA256="a" * 64)
        for index in range(3):
            result = self.launch(str(index), env=env)
            args = json.loads(result.stdout)
            self.assertEqual(args[:4], ["-u", "-B", "src/Matched_Encoding_Evaluation.py", "run"])
            self.assertEqual(args[args.index("--shard-index") + 1], str(index))
            self.assertEqual(Path(args[args.index("--uwrmaxsat-bin") + 1]).resolve(),
                             (self.checkout / "bin/uwrmaxsat").resolve())
            self.assertIn("--allow-dirty", args)

    def test_original_environment_file_and_virtualenv_are_used(self):
        executable = self.checkout / ".venv-ubuntu/bin/python"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        executable.chmod(0o755)
        env_file = self.checkout / "journal_gcp.env"
        env_file.write_text('JOURNAL_VENV=.venv-ubuntu\nUWRMAXSAT_BIN=bin/uwrmaxsat\n'
                            'UWRMAXSAT_SHA256=' + "a" * 64 + '\nALLOW_DIRTY=0\n')
        env = {key: value for key, value in self.env.items() if key != "JOURNAL_PYTHON"}
        try:
            args = json.loads(self.launch("0", env=env).stdout)
            self.assertEqual(args[3], "run")
            self.assertIn("--allow-dirty", args)
        finally:
            env_file.unlink()
            shutil.rmtree(executable.parent.parent)

    def test_modified_runtime_source_is_rejected_without_overwriting_it(self):
        path = self.runtime / "src/Matched_Encoding_Evaluation.py"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n# modified source\n")
            result = self.launch("plan", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("frozen runtime file is missing or changed", result.stderr)
            self.assertTrue(path.read_bytes().endswith(b"# modified source\n"))
        finally:
            path.write_bytes(original)

    def test_existing_unrelated_directory_is_preserved(self):
        other = self.directory / "unrelated"
        other.mkdir()
        marker = other / "keep.txt"
        marker.write_text("keep")
        result = self.launch("plan", env=dict(self.env, MATCHED_RUNTIME_DIR=str(other)), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("existing directory is not the frozen campaign", result.stderr)
        self.assertEqual(marker.read_text(), "keep")

    def test_prepare_uses_existing_validated_preparation(self):
        result = self.launch("prepare")
        self.assertEqual(json.loads(result.stdout), self.report)


class BootstrapValidationTests(unittest.TestCase):
    def test_corrupted_archive_is_rejected_before_creating_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            archive = project / bootstrap.ARCHIVE
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"corrupted archive")
            runtime = Path(directory) / "runtime"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                bootstrap.ensure_runtime(project, runtime)
            self.assertFalse(runtime.exists())

    def test_tar_path_traversal_links_and_duplicates_are_rejected(self):
        for names, kind in ((["../outside"], tarfile.REGTYPE),
                            (["/absolute"], tarfile.REGTYPE),
                            (["link"], tarfile.SYMTYPE),
                            (["same", "same"], tarfile.REGTYPE)):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name in names:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    archive.addfile(member)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                with self.assertRaisesRegex(ValueError, "invalid or duplicate"):
                    bootstrap.checked_members(archive)

    def test_python_virtualenv_symlink_is_not_resolved_to_system_python(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            self.assertEqual(bootstrap.absolute_argument("venv/bin/python", Path(directory)),
                             str(executable))
            self.assertNotEqual(str(executable), str(executable.resolve()))


if __name__ == "__main__":
    unittest.main()
