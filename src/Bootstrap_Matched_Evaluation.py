"""Install and enter the published frozen campaign using only the standard library.

The GitHub checkout is a launcher. Computation runs in a separate worktree at
the base commit recorded by the archive, with its exact exported source files.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = Path("outputs/matched-encoding-v2-gcp.tar.gz")
ARCHIVE_SHA256 = "f99cbe2aa25a7d30c090b828de5392519f94c9baec5789ccffc4b17aab4a7263"
BUNDLE = Path("outputs/journal/matched-encoding-v2")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Only regular files under relative paths may be installed."""
    members = archive.getmembers()
    names = set()
    for member in members:
        path = Path(member.name)
        if (path.is_absolute() or ".." in path.parts or not member.isfile()
                or member.name in names):
            raise ValueError(f"invalid or duplicate package member: {member.name}")
        names.add(member.name)
    return members


def verify_runtime(runtime: Path, campaign: dict, report: dict) -> None:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=runtime, text=True
    ).strip()
    if commit != campaign["frozen_sources"]["git_commit"]:
        raise ValueError(f"runtime base commit changed: {runtime}")
    expected = dict(campaign["frozen_sources"]["code_files"])
    expected.update({str(BUNDLE / name): value
                     for name, value in report["bundle_hashes"].items()})
    for name, sha256 in expected.items():
        path = runtime / name
        if not path.is_file() or file_hash(path) != sha256:
            raise ValueError(f"frozen runtime file is missing or changed: {path}")
    actual_sources = {path.relative_to(runtime).as_posix()
                      for path in (runtime / "src").glob("*.py")}
    expected_sources = {name for name in expected
                        if name.startswith("src/") and name.endswith(".py")}
    if actual_sources != expected_sources:
        raise ValueError(f"unexpected Python source files in frozen runtime: {runtime}")


def ensure_runtime(project: Path, runtime: Path) -> Path:
    """Install once; subsequent calls validate without rewriting code or results."""
    if runtime == project or project in runtime.parents:
        raise ValueError("MATCHED_RUNTIME_DIR must be outside the launcher checkout")
    archive_path = project / ARCHIVE
    if not archive_path.is_file() or file_hash(archive_path) != ARCHIVE_SHA256:
        raise ValueError("published GCP package is missing or has a different SHA-256; git pull the campaign branch")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = checked_members(archive)
        campaign = json.load(archive.extractfile(str(BUNDLE / "campaign.json")))
        report = json.load(archive.extractfile(str(BUNDLE / "preparation_report.json")))
        runtime.parent.mkdir(parents=True, exist_ok=True)
        lock_path = runtime.parent / ("." + runtime.name + ".bootstrap.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not runtime.exists():
                print(f"Preparing frozen runtime: {runtime}", file=sys.stderr, flush=True)
                subprocess.run(["git", "worktree", "add", "--detach", str(runtime),
                                campaign["frozen_sources"]["git_commit"]],
                               cwd=project, check=True, stdout=sys.stderr)
                # The archive hash and regular, relative members are checked above.
                archive.extractall(runtime, members=members)
            elif not (runtime / BUNDLE / "campaign.json").is_file():
                raise ValueError(f"existing directory is not the frozen campaign: {runtime}; "
                                 "set MATCHED_RUNTIME_DIR to a new directory")
            verify_runtime(runtime, campaign, report)
    return runtime


def absolute_argument(value: str, cwd: Path) -> str:
    path = Path(value).expanduser()
    # Keep a virtualenv's executable symlink: resolving it loses the environment.
    return os.path.abspath(cwd / path)


def child_environment(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("JOURNAL_PYTHON", "PLAN_PYTHON"):
        if key in env:
            value = env[key]
            executable = shutil.which(value) if "/" not in value else None
            env[key] = executable or absolute_argument(value, cwd)
    for key in ("UWRMAXSAT_BIN", "MATCHED_OUTPUT", "MATCHED_MERGED_OUTPUT"):
        if env.get(key):
            value = env[key]
            executable = shutil.which(value) if key == "UWRMAXSAT_BIN" and "/" not in value else None
            env[key] = executable or absolute_argument(value, cwd)
    return env


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv) or ["plan"]
    worker = args[0] == "--worker"
    if worker:
        args = args[1:] or ["plan"]
    allowed = ("prepare", "plan", "run", "package") if worker else (
        "prepare", "plan", "0", "1", "2", "merge", "analyze", "validate")
    if args[0] not in allowed:
        print("Usage: run_matched_encoding_three_vms.sh plan|0|1|2|validate INDEX|merge DIR0 DIR1 DIR2|analyze", file=sys.stderr)
        return 2
    if args[0] == "merge" and len(args) != 4:
        raise ValueError("merge requires all three shard directories")
    if args[0] == "validate" and (len(args) != 2 or args[1] not in ("0", "1", "2")):
        raise ValueError("validate requires shard index 0, 1, or 2")
    cwd = Path.cwd()
    env = child_environment(cwd)
    runtime = Path(os.environ.get("MATCHED_RUNTIME_DIR", str(PROJECT_ROOT.parent / "b2b-matched-v2"))).expanduser().resolve()
    ensure_runtime(PROJECT_ROOT, runtime)
    python = env.get("PLAN_PYTHON", env.get("JOURNAL_PYTHON", sys.executable))
    print(f"Frozen runtime: {runtime}", file=sys.stderr, flush=True)
    if worker:
        for option in ("--output", "--archive", "--uwrmaxsat-bin"):
            if option in args:
                index = args.index(option) + 1
                if index < len(args):
                    args[index] = absolute_argument(args[index], cwd)
        command = [python, "-B", "src/Matched_Encoding_Evaluation.py", *args]
    elif args[0] == "validate":
        bundle = Path(env.get("MATCHED_OUTPUT", str(runtime / BUNDLE)))
        command = [python, "-B", "src/Validate_Journal_Run.py", "--output",
                   str(bundle / f"shard-{args[1]}-of-3"), "--allow-incomplete",
                   "--allow-dirty", "--allow-environment-drift"]
    else:
        if args[0] == "prepare":
            # The published archive already contains the validated preparation.
            args[0] = "plan"
        if args[0] == "merge":
            args[1:] = [absolute_argument(value, cwd) for value in args[1:]]
        command = ["bash", "scripts/run_matched_encoding_three_vms.sh", *args]
    return subprocess.call(command, cwd=runtime, env=env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
