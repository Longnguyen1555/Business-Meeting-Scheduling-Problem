"""Copy a finished VM shard into a Git-ready archive in the launcher checkout.

This standard-library command never imports solver code, bootstraps a worktree,
changes result records, stages files, or creates Git commits.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tarfile
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path("outputs/journal/matched-encoding-v2")
RESULTS = Path("outputs/journal/matched-encoding-v2-results")
TERMINAL = {"OPTIMAL", "UNSAT", "TIMEOUT"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def result_inventory(source: Path, index: int, allow_incomplete: bool) -> dict:
    required = ("plan.json", "environment.json", "raw/results.jsonl", "normalized/detailed.csv")
    for name in required:
        if not (source / name).is_file():
            raise ValueError(f"missing result file: {source / name}")
    plan = json.loads((source / "plan.json").read_text())
    environment = json.loads((source / "environment.json").read_text())
    if (environment.get("execution_shard_count") != 3
            or environment.get("execution_shard_indices") != [index]):
        raise ValueError(f"environment does not identify shard {index} of 3")
    if environment.get("plan_sha256") != plan.get("plan_sha256"):
        raise ValueError("environment and plan SHA-256 differ")
    contents = sorted({str(job["instance_content_id"]) for job in plan["jobs"]})
    assigned = {content for rank, content in enumerate(contents) if rank % 3 == index}
    expected = {job["run_key"] for job in plan["jobs"] if job["instance_content_id"] in assigned}
    latest = {}
    raw_count = 0
    with (source / "raw/results.jsonl").open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = record["run_key"]
                attempt = int(record["attempt"])
                if key in latest and attempt <= int(latest[key]["attempt"]):
                    raise ValueError("attempt numbers are not increasing")
                if record["row"].get("run_key") != key:
                    raise ValueError("row run_key differs from record")
                latest[key] = record
                raw_count += 1
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f"invalid record at JSONL line {number}: {error}") from error
    unexpected = set(latest) - expected
    if unexpected:
        raise ValueError(f"shard contains {len(unexpected)} unassigned run keys")
    outcomes = Counter(record["row"]["status"] for record in latest.values())
    missing = len(expected - set(latest))
    nonterminal = sum(count for status, count in outcomes.items() if status not in TERMINAL)
    if (missing or nonterminal) and not allow_incomplete:
        raise ValueError(f"shard has {missing} missing and {nonterminal} nonterminal cells; "
                         "use --allow-incomplete to archive diagnostics without treating it as complete")
    return {"plan_sha256": plan["plan_sha256"], "shard_index": index,
            "expected_cells": len(expected), "observed_cells": len(latest),
            "raw_attempts": raw_count, "missing_cells": missing,
            "nonterminal_cells": nonterminal, "outcomes": dict(sorted(outcomes.items())),
            "complete": not (missing or nonterminal),
            "validation_scope": "record coverage and statuses; independent scientific validation follows after transfer"}


def source_files(source: Path) -> dict[str, dict]:
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"result tree contains a symlink: {path}")
        if path.name == ".worker.lock" or path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported result entry: {path}")
        name = path.relative_to(source).as_posix()
        if name == "export_manifest.json":
            raise ValueError("source already contains a reserved export_manifest.json")
        files[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
    return files


def verify_archive(archive: Path, prefix: str, files: dict) -> dict:
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        expected = {f"{prefix}/{name}" for name in files} | {f"{prefix}/export_manifest.json"}
        if len(members) != len(expected) or {member.name for member in members} != expected:
            raise ValueError("archive file inventory differs from source")
        if any(not member.isfile() for member in members):
            raise ValueError("archive contains non-file members")
        manifest = json.load(stream.extractfile(f"{prefix}/export_manifest.json"))
        if manifest["files"] != files:
            raise ValueError("archive manifest differs from source")
        for name, metadata in files.items():
            digest = hashlib.sha256()
            with stream.extractfile(f"{prefix}/{name}") as data:
                for block in iter(lambda: data.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != metadata["sha256"]:
                raise ValueError(f"archived content differs: {name}")
    return manifest


def export_shard(source: Path, archive: Path, index: int, *, allow_incomplete=False) -> dict:
    source, archive = source.resolve(), archive.resolve()
    if not source.is_dir():
        raise ValueError(f"result shard does not exist: {source}; use --source for a custom location")
    if source == archive or source in archive.parents:
        raise ValueError("archive must be outside the source shard")
    # Match the runner's lock without creating or altering any source file.
    lock_path = source / ".worker.lock"
    lock = lock_path.open("rb") if lock_path.exists() else None
    temporary = None
    try:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("shard worker is still active; wait for it to finish before export") from error
        inventory = result_inventory(source, index, allow_incomplete)
        files = source_files(source)
        prefix = f"shard-{index}-of-3"
        if archive.exists():
            verify_archive(archive, prefix, files)
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            manifest = {"schema_version": 1, "exported_utc": datetime.now(timezone.utc).isoformat(),
                        "source_directory": str(source), "inventory": inventory, "files": files}
            with tempfile.NamedTemporaryFile(prefix=".matched-export-", suffix=".tar.gz",
                                             dir=archive.parent, delete=False) as stream:
                temporary = Path(stream.name)
            with tarfile.open(temporary, "w:gz") as stream:
                for name in files:
                    stream.add(source / name, arcname=f"{prefix}/{name}", recursive=False)
                data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
                member = tarfile.TarInfo(f"{prefix}/export_manifest.json")
                member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
            if source_files(source) != files:
                raise ValueError("source files changed during export; retry after the worker stops")
            verify_archive(temporary, prefix, files)
            if temporary.stat().st_size >= 100 * 1024 * 1024:
                raise ValueError("compressed shard exceeds GitHub's 100 MiB file limit; transfer this shard as an external artifact")
            # Publish atomically without replacing an existing export.
            os.link(temporary, archive)
        return {"archive": str(archive), "archive_sha256": file_hash(archive),
                "archive_bytes": archive.stat().st_size, "file_count": len(files), **inventory}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if lock is not None:
            lock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=int, choices=(0, 1, 2))
    parser.add_argument("--source", type=Path, help="existing shard directory, if outside the default runtime")
    parser.add_argument("--archive", type=Path, help="new archive path; existing differing archives are never replaced")
    parser.add_argument("--allow-incomplete", action="store_true", help="export missing/error cells for diagnosis")
    args = parser.parse_args(argv)
    runtime = Path(os.environ.get("MATCHED_RUNTIME_DIR", str(PROJECT_ROOT.parent / "b2b-matched-v2"))).expanduser()
    bundle = Path(os.environ.get("MATCHED_OUTPUT", str(runtime / BUNDLE))).expanduser()
    source = args.source or bundle / f"shard-{args.index}-of-3"
    archive = args.archive or PROJECT_ROOT / RESULTS / f"shard-{args.index}-of-3.tar.gz"
    report = export_shard(source, archive, args.index, allow_incomplete=args.allow_incomplete)
    print(json.dumps(report, indent=2, sort_keys=True))
    try:
        relative = archive.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return 0
    path = shlex.quote(relative)
    print(f"\nFrom the launcher repository:\ngit add -- {path}\n"
          f"git commit -m 'Add matched evaluation results for shard {args.index}'\n"
          "git pull --rebase origin journal-compact-objectives\n"
          "git push origin HEAD:journal-compact-objectives", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
