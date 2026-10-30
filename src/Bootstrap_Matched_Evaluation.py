"""Verified, self-repairing launcher for the published matched-encoding campaign."""
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


def checked_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members = {}
    for member in archive.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or not member.isfile() or member.name in members:
            raise ValueError(f"invalid or duplicate archive member: {member.name}")
        members[member.name] = member
    return members


def archive_json(archive: tarfile.TarFile, members: dict, name: str) -> dict:
    member = members.get(name)
    if member is None:
        raise ValueError(f"published archive is missing {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"cannot read {name} from archive")
    with stream:
        return json.load(stream)


def restore_member(archive: tarfile.TarFile, member: tarfile.TarInfo, runtime: Path) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"cannot extract {member.name}")
    target = runtime / member.name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".bootstrap-tmp")
    with source, temporary.open("wb") as dest:
        shutil.copyfileobj(source, dest)
        dest.flush()
        os.fsync(dest.fileno())
    os.chmod(temporary, member.mode & 0o777)
    temporary.replace(target)


def verify_runtime(runtime: Path, campaign: dict, report: dict) -> None:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runtime, text=True).strip()
    expected_commit = campaign["frozen_sources"]["git_commit"]
    if commit != expected_commit:
        raise ValueError(f"runtime base commit mismatch: expected {expected_commit}, got {commit}")
    expected = dict(campaign["frozen_sources"]["code_files"])
    expected.update({str(BUNDLE / name): digest for name, digest in report["bundle_hashes"].items()})
    for relative, digest in expected.items():
        path = runtime / relative
        if not path.is_file() or file_hash(path) != digest:
            raise ValueError(f"restored frozen file failed verification: {path}")
    actual_sources = {p.relative_to(runtime).as_posix() for p in (runtime / "src").glob("*.py")}
    expected_sources = {name for name in expected if name.startswith("src/") and name.endswith(".py")}
    if actual_sources != expected_sources:
        raise ValueError(
            "unexpected Python files in frozen runtime: "
            f"extra={sorted(actual_sources - expected_sources)}, missing={sorted(expected_sources - actual_sources)}"
        )


def ensure_runtime(project: Path, runtime: Path) -> None:
    if runtime == project or project in runtime.parents:
        raise ValueError("MATCHED_RUNTIME_DIR must be outside the launcher checkout")
    archive_path = project / ARCHIVE
    if not archive_path.is_file() or file_hash(archive_path) != ARCHIVE_SHA256:
        raise ValueError("published GCP archive is missing or SHA-256 differs; update the launcher branch")

    with tarfile.open(archive_path, "r:gz") as archive:
        members = checked_members(archive)
        campaign_name = str(BUNDLE / "campaign.json")
        report_name = str(BUNDLE / "preparation_report.json")
        campaign = archive_json(archive, members, campaign_name)
        report = archive_json(archive, members, report_name)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        lock_path = runtime.parent / ("." + runtime.name + ".bootstrap.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not runtime.exists():
                print(f"Preparing frozen runtime: {runtime}", file=sys.stderr, flush=True)
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(runtime), campaign["frozen_sources"]["git_commit"]],
                    cwd=project, check=True, stdout=sys.stderr,
                )
            # Restore only immutable package files. Existing shard directories,
            # raw JSONL records, logs and run outputs are intentionally untouched.
            required = set(campaign["frozen_sources"]["code_files"])
            required.update(str(BUNDLE / name) for name in report["bundle_hashes"])
            required.update({campaign_name, report_name})
            absent = sorted(required - set(members))
            if absent:
                raise ValueError(f"archive lacks required frozen files: {absent}")
            for name in sorted(required):
                restore_member(archive, members[name], runtime)
            verify_runtime(runtime, campaign, report)


def normalize_path(value: str, cwd: Path) -> str:
    if "/" not in value:
        import shutil as _shutil
        found = _shutil.which(value)
        if found:
            return found
    return os.path.abspath(cwd / Path(value).expanduser())


def child_environment(cwd: Path) -> dict:
    env = os.environ.copy()
    for key in ("JOURNAL_PYTHON", "PLAN_PYTHON", "UWRMAXSAT_BIN", "MATCHED_OUTPUT", "MATCHED_MERGED_OUTPUT"):
        if env.get(key):
            env[key] = normalize_path(env[key], cwd)
    return env


def apply_local_solver_pin(runtime: Path, solver: str, configured_sha: str) -> None:
    if not solver or not configured_sha:
        raise ValueError("set UWRMAXSAT_BIN and UWRMAXSAT_SHA256 in journal_gcp.env")
    binary = Path(solver).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"UWrMaxSAT executable is missing/not executable: {binary}")
    actual = file_hash(binary)
    configured = configured_sha.strip().lower()
    if len(configured) != 64 or actual != configured:
        raise ValueError(f"local solver SHA mismatch: configured={configured}, actual={actual}")

    bundle = runtime / BUNDLE
    campaign_path = bundle / "campaign.json"
    report_path = bundle / "preparation_report.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # The sole permitted per-VM bundle variation is the checked local solver pin.
    campaign["uwrmaxsat_sha256"] = actual
    campaign_path.write_text(json.dumps(campaign, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["bundle_hashes"]["campaign.json"] = file_hash(campaign_path)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv) or ["plan"]
    if args[0] not in ("plan", "0", "1", "2", "validate", "merge"):
        raise ValueError("expected plan, 0, 1, 2, validate INDEX, or merge DIR0 DIR1 DIR2")
    if args[0] == "validate" and (len(args) != 2 or args[1] not in ("0", "1", "2")):
        raise ValueError("validate requires shard index 0, 1, or 2")
    if args[0] == "merge" and len(args) != 4:
        raise ValueError("merge requires three shard directories")

    cwd = Path.cwd()
    env = child_environment(cwd)
    runtime = Path(os.environ.get("MATCHED_RUNTIME_DIR", str(PROJECT_ROOT.parent / "b2b-matched-v2"))).expanduser().resolve()
    ensure_runtime(PROJECT_ROOT, runtime)

    if args[0] in ("0", "1", "2"):
        apply_local_solver_pin(runtime, env.get("UWRMAXSAT_BIN", ""), env.get("UWRMAXSAT_SHA256", ""))
    elif env.get("UWRMAXSAT_BIN") and env.get("UWRMAXSAT_SHA256"):
        apply_local_solver_pin(runtime, env["UWRMAXSAT_BIN"], env["UWRMAXSAT_SHA256"])

    python = env.get("JOURNAL_PYTHON") or env.get("PLAN_PYTHON") or sys.executable
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"Python executable missing/not executable: {python}")
    print(f"Frozen runtime: {runtime}", file=sys.stderr, flush=True)

    if args[0] == "plan":
        code = "import sys; sys.path.insert(0,'src'); from Matched_Encoding_Evaluation import main; raise SystemExit(main(['plan']))"
        command = [python, "-B", "-c", code]
    elif args[0] in ("0", "1", "2"):
        code = (
            "import os,sys; sys.path.insert(0,'src'); from Matched_Encoding_Evaluation import main; "
            f"a=['run','--shard-index','{args[0]}','--uwrmaxsat-bin',sys.argv[1],"
            "'--uwrmaxsat-sha256',sys.argv[2]]; "
            "a += ['--allow-dirty'] if os.environ.get('ALLOW_DIRTY') == '1' else []; "
            "a += ['--retry-errors'] if os.environ.get('RETRY_ERRORS') == '1' else []; "
            "raise SystemExit(main(a))"
        )
        command = [python, "-B", "-c", code, env["UWRMAXSAT_BIN"], env["UWRMAXSAT_SHA256"].lower()]
    elif args[0] == "validate":
        output = Path(env.get("MATCHED_OUTPUT", str(runtime / BUNDLE)))
        command = [python, "-B", "src/Validate_Journal_Run.py", "--output", str(output / f"shard-{args[1]}-of-3"), "--allow-incomplete", "--allow-dirty", "--allow-environment-drift"]
    else:
        output = Path(env.get("MATCHED_MERGED_OUTPUT", str(runtime / BUNDLE / "merged")))
        code = "import sys; sys.path.insert(0,'src'); from Merge_Journal_Shards import main; raise SystemExit(main())"
        command = [python, "-B", "-c", code]
        for shard in args[1:]:
            command.extend(["--input", normalize_path(shard, cwd)])
        command.extend(["--output", str(output), "--allow-mixed-environments"])
    return subprocess.call(command, cwd=runtime, env=env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
