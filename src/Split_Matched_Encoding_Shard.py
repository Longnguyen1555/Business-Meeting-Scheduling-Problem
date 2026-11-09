"""Partition one interrupted execution shard into disjoint resume shards.

The source JSONL is copied append-only. Existing terminal records are retained
and their execution-shard metadata is rewritten for the new partition. No
solver is started by this command.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

TERMINAL = {"OPTIMAL", "UNSAT", "TIMEOUT"}
POLICY = "content_rank_round_robin_v1"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_records(source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_attempts: dict[str, int] = {}
    with (source / "raw" / "results.jsonl").open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = str(record["run_key"])
                attempt = int(record["attempt"])
                if record.get("row", {}).get("run_key") != key:
                    raise ValueError("row run_key differs from record")
                if key in seen_attempts and attempt <= seen_attempts[key]:
                    raise ValueError("attempt numbers are not strictly increasing")
                seen_attempts[key] = attempt
                records.append(record)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid source record at line {line_number}: {error}") from error
    return records


def assignments(plan: dict[str, Any], shard_count: int) -> dict[str, int]:
    content_ids = sorted({str(job["instance_content_id"]) for job in plan["jobs"]})
    content_shards = {content_id: rank % shard_count for rank, content_id in enumerate(content_ids)}
    return {str(job["run_key"]): content_shards[str(job["instance_content_id"])] for job in plan["jobs"]}


def copy_log(source: Path, destination: Path, row: dict[str, Any], key: str) -> None:
    original = str(row.get("campaign_log", ""))
    if not original:
        return
    relative = Path(original)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe campaign log path for {key}: {original}")
    candidates = [source / relative]
    candidates.extend((source / "logs").glob(f"**/{relative.name}"))
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if not candidates:
        return
    selected = candidates[0]
    target = destination / "logs" / "reused" / "parent-shard-0" / relative.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(selected, target)
    row["campaign_log"] = target.relative_to(destination).as_posix()


def split_shard(
    source: Path,
    output: Path,
    *,
    parent_index: int = 0,
    parent_count: int = 3,
    new_count: int = 9,
    new_indices: tuple[int, ...] = (0, 3, 6),
) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    if parent_count <= 0 or new_count <= 0:
        raise ValueError("shard counts must be positive")
    if not new_indices or any(index < 0 or index >= new_count for index in new_indices):
        raise ValueError("new shard indices are outside the requested shard count")
    if len(set(new_indices)) != len(new_indices):
        raise ValueError("new shard indices must be distinct")
    if any(index % parent_count != parent_index for index in new_indices):
        raise ValueError("new shard indices do not refine the parent shard")
    if source == output or source in output.parents:
        raise ValueError("split output must be outside the source shard")
    for name in ("plan.json", "raw/results.jsonl"):
        if not (source / name).is_file():
            raise ValueError(f"source shard is missing {name}")
    plan = read_json(source / "plan.json")
    target_assignments = assignments(plan, new_count)
    parent_assignments = assignments(plan, parent_count)
    expected_parent = {
        str(job["run_key"]) for job in plan["jobs"]
        if parent_assignments[str(job["run_key"])] == parent_index
    }
    records = read_records(source)
    all_source_keys = {str(record["run_key"]) for record in records}
    unexpected = all_source_keys - expected_parent
    if unexpected:
        raise ValueError(f"source shard contains {len(unexpected)} records outside parent shard")
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        latest[str(record["run_key"])] = record
    nonterminal = [key for key, record in latest.items() if record.get("row", {}).get("status") not in TERMINAL]
    if nonterminal:
        raise ValueError(f"source contains {len(nonterminal)} nonterminal records; finish or diagnose them first")
    selected = {index: {key for key, shard in target_assignments.items() if shard == index} for index in new_indices}
    source_sha = sha256(source / "raw" / "results.jsonl")
    reports = []
    for index in new_indices:
        destination = output / f"shard-{index}-of-{new_count}"
        if destination.exists() and any(destination.iterdir()):
            raise ValueError(f"refusing to overwrite nonempty split shard: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "plan.json", plan)
        target_keys = selected[index]
        target_records = []
        for record in records:
            key = str(record["run_key"])
            if key not in target_keys:
                continue
            copied = json.loads(json.dumps(record))
            row = copied["row"]
            row["execution_shard_count"] = new_count
            row["execution_shard_indices"] = str(index)
            row["execution_shard_index"] = index
            row["execution_shard_policy"] = POLICY
            row["split_parent_execution_shard_count"] = parent_count
            row["split_parent_execution_shard_index"] = parent_index
            row["split_parent_results_sha256"] = source_sha
            copy_log(source, destination, row, key)
            target_records.append(copied)
        target_records.sort(key=lambda record: (str(record["run_key"]), int(record["attempt"])))
        raw_path = destination / "raw" / "results.jsonl"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("w", encoding="utf-8") as stream:
            for record in target_records:
                stream.write(canonical(record) + "\n")
        target_jobs = {key for key, shard in target_assignments.items() if shard == index}
        reports.append({
            "shard_index": index,
            "expected_jobs": len(target_jobs),
            "copied_records": len({str(record["run_key"]) for record in target_records}),
            "pending_jobs": len(target_jobs - {str(record["run_key"]) for record in target_records}),
            "source_records_sha256": source_sha,
            "path": str(destination),
        })
    report = {
        "schema_version": 1,
        "parent_shard_index": parent_index,
        "parent_shard_count": parent_count,
        "new_shard_count": new_count,
        "new_shard_indices": list(new_indices),
        "plan_sha256": plan["plan_sha256"],
        "source": str(source),
        "source_records_sha256": source_sha,
        "source_record_count": len(records),
        "source_pending_count": len(expected_parent - set(latest)),
        "shards": reports,
    }
    write_json(output / "split_manifest.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent-index", type=int, default=0)
    parser.add_argument("--parent-count", type=int, default=3)
    parser.add_argument("--new-count", type=int, default=9)
    parser.add_argument("--new-index", type=int, action="append", required=True)
    args = parser.parse_args(argv)
    report = split_shard(args.source, args.output, parent_index=args.parent_index,
                         parent_count=args.parent_count, new_count=args.new_count,
                         new_indices=tuple(args.new_index))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
