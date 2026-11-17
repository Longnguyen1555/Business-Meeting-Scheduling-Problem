"""Wait for an existing worker, then archive its complete results without solving."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import sys
import time

from Export_Matched_Results import export_shard, file_hash


def wait_for_worker(pid: int, source: Path, interval: float = 30) -> None:
    if pid <= 0 or interval <= 0:
        raise ValueError("worker PID and polling interval must be positive")
    process = Path("/proc") / str(pid)
    while (process / "cmdline").exists():
        try:
            args = (process / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            break
        values = [item.decode(errors="replace") for item in args]
        if not values or not values[0]:
            break  # An exited worker can remain as a zombie until reaped.
        if "--output-dir" not in values or str(source) not in values:
            raise ValueError("PID identifies a different process; refusing to wait or signal it")
        time.sleep(interval)


def finalize(source: Path, archive: Path, index: int, count: int, config: Path) -> dict:
    source = source.resolve()
    status = json.loads((source / "campaign_status.json").read_text())
    if status.get("interrupted") or status.get("error_rows") != 0:
        raise ValueError("worker was interrupted or has ERROR rows; preserve results for diagnosis")
    environment = json.loads((source / "environment.json").read_text())
    if file_hash(config) != environment.get("config_sha256"):
        raise ValueError("current campaign configuration differs from the worker's recorded SHA-256")
    evidence = source / "provenance" / "campaign.json"
    evidence.parent.mkdir(exist_ok=True)
    if evidence.exists():
        if file_hash(evidence) != environment["config_sha256"]:
            raise ValueError("existing configuration evidence differs; refusing to replace it")
    else:
        shutil.copy2(config, evidence)
    # Coverage validation rejects missing/nonterminal cells even if the worker's
    # status file was incomplete or stale. TIMEOUT is a valid terminal status.
    return export_shard(source, archive, index, shard_count=count)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, default=9)
    args = parser.parse_args(argv)
    source = args.source.resolve()
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.archive.with_name(args.archive.name + ".finalizer.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f"Waiting for worker PID {args.worker_pid}: {source}", flush=True)
        wait_for_worker(args.worker_pid, source)
        report = finalize(source, args.archive, args.index, args.shard_count, args.config)
        receipt = args.archive.with_name(args.archive.name + ".receipt.json")
        receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
