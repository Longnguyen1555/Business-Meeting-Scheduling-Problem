from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from Journal_Experiment import (
    EXECUTION_SHARD_POLICY,
    TERMINAL_STATUSES,
    _read_jsonl,
    _write_json,
    canonical_json,
    execution_shard_assignments,
    latest_attempts,
    materialize_results,
)
from Validate_Journal_Run import validate_campaign


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as stream:
        return [dict(row) for row in csv.DictReader(stream)]


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".recovery.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _check_csv_metadata(
    row: dict[str, str],
    job: dict[str, Any],
    *,
    plan_sha256: str,
    shard_count: int,
    shard_indices: tuple[int, ...],
    shard_index: int,
) -> None:
    expected = {
        "run_key": job["run_key"],
        "experiment_block": job["experiment_block"],
        "planned_configuration_id": job["planned_configuration_id"],
        "repetition": job["repetition"],
        "run_order": job["run_order"],
        "campaign_plan_sha256": plan_sha256,
        "instance_content_id": job["instance_content_id"],
        "instance_sha256": job["instance_sha256"],
        "base_lineage_id": job["base_lineage_id"],
        "objective_mode": job["configuration"].get("objective_mode", ""),
        "execution_shard_count": shard_count,
        "execution_shard_indices": ",".join(str(value) for value in shard_indices),
        "execution_shard_index": shard_index,
        "execution_shard_policy": EXECUTION_SHARD_POLICY,
    }
    mismatches = [
        field
        for field, value in expected.items()
        if str(row.get(field, "")) != str(value)
    ]
    if mismatches:
        raise ValueError(
            f"{job['run_key']}: normalized metadata mismatch for "
            + ", ".join(mismatches)
        )
    if row.get("status") not in TERMINAL_STATUSES:
        raise ValueError(
            f"{job['run_key']}: normalized row is not terminal: "
            f"{row.get('status')!r}"
        )
    try:
        attempt = int(row.get("attempt", ""))
    except ValueError as exc:
        raise ValueError(f"{job['run_key']}: invalid attempt") from exc
    if attempt < 1:
        raise ValueError(f"{job['run_key']}: invalid attempt {attempt}")


def recover_results(
    output: Path,
    *,
    allow_dirty: bool = False,
    allow_environment_drift: bool = False,
) -> dict[str, Any]:
    """Recover missing append-only records from a previously materialized CSV."""

    output = output.resolve()
    plan = _read_json(output / "plan.json")
    environment_path = output / "environment.json"
    environment = _read_json(environment_path)
    normalized_path = output / "normalized" / "detailed.csv"
    normalized_sha256 = _file_sha256(normalized_path)
    normalized_rows = _read_csv(normalized_path)
    raw_path = output / "raw" / "results.jsonl"
    existing_records = _read_jsonl(raw_path)
    existing_latest = latest_attempts(existing_records)

    jobs = {str(job["run_key"]): job for job in plan.get("jobs", [])}
    if len(jobs) != int(plan.get("job_count", -1)):
        raise ValueError("plan job_count does not match unique run keys")
    shard_count = int(environment.get("execution_shard_count", 1))
    shard_indices = tuple(
        int(value) for value in environment.get("execution_shard_indices", [0])
    )
    if not shard_indices:
        raise ValueError("environment has no assigned shard indices")
    assignments = execution_shard_assignments(plan, shard_count)
    assigned_keys = {
        run_key
        for run_key, shard_index in assignments.items()
        if shard_index in shard_indices
    }

    csv_by_key: dict[str, dict[str, str]] = {}
    for row in normalized_rows:
        run_key = str(row.get("run_key", ""))
        if not run_key or run_key in csv_by_key:
            raise ValueError(
                "normalized/detailed.csv has blank or duplicate run keys"
            )
        if run_key not in jobs:
            raise ValueError(f"normalized CSV has unplanned run key {run_key!r}")
        if run_key not in assigned_keys:
            raise ValueError(
                f"normalized CSV row belongs to an unassigned shard: {run_key!r}"
            )
        _check_csv_metadata(
            row,
            jobs[run_key],
            plan_sha256=str(plan["plan_sha256"]),
            shard_count=shard_count,
            shard_indices=shard_indices,
            shard_index=assignments[run_key],
        )
        csv_by_key[run_key] = row

    existing_extra = set(existing_latest) - assigned_keys
    if existing_extra:
        raise ValueError(
            f"raw results contain {len(existing_extra)} unassigned run keys"
        )
    missing_from_csv = assigned_keys - set(csv_by_key)
    if missing_from_csv:
        raise ValueError(
            "normalized CSV cannot recover every assigned result; missing "
            f"{len(missing_from_csv)} run keys"
        )

    recovered_utc = _utc_now()
    recovered_records: list[dict[str, Any]] = []
    for run_key in sorted(
        assigned_keys - set(existing_latest),
        key=lambda key: int(jobs[key]["run_order"]),
    ):
        row: dict[str, Any] = dict(csv_by_key[run_key])
        recovered_records.append(
            {
                "run_key": run_key,
                "attempt": int(row["attempt"]),
                "completed_utc": recovered_utc,
                "raw_record_origin": "recovered_from_normalized_csv",
                "recovered_from_normalized_sha256": normalized_sha256,
                "recovered_utc": recovered_utc,
                "recovery_source": "normalized/detailed.csv",
                "recovery_source_sha256": normalized_sha256,
                "row": row,
            }
        )

    if not recovered_records:
        return {
            "existing_records": len(existing_latest),
            "recovered_records": 0,
            "assigned_jobs": len(assigned_keys),
            "normalized_sha256": normalized_sha256,
        }

    candidate_records = [*existing_records, *recovered_records]
    with tempfile.TemporaryDirectory(prefix="b2b_journal_recovery_") as temp:
        candidate = Path(temp)
        shutil.copy2(output / "plan.json", candidate / "plan.json")
        shutil.copy2(environment_path, candidate / "environment.json")
        _write_jsonl_atomic(candidate / "raw" / "results.jsonl", candidate_records)
        materialize_results(candidate)
        errors, _ = validate_campaign(
            candidate,
            allow_dirty=allow_dirty,
            allow_incomplete=True,
            allow_environment_drift=allow_environment_drift,
        )
        if errors:
            raise ValueError(
                "reconstructed records failed validation: "
                + "; ".join(errors[:10])
            )

    backup_path = raw_path.with_name("results.before-normalized-recovery.jsonl")
    if raw_path.is_file() and not backup_path.exists():
        shutil.copy2(raw_path, backup_path)
    _write_jsonl_atomic(raw_path, candidate_records)
    rows = materialize_results(output)

    environment["normalized_result_recovery"] = {
        "recovered_utc": recovered_utc,
        "source": "normalized/detailed.csv",
        "source_sha256": normalized_sha256,
        "existing_records": len(existing_latest),
        "recovered_records": len(recovered_records),
        "assigned_jobs": len(assigned_keys),
    }
    _write_json(environment_path, environment)
    _write_json(
        output / "campaign_status.json",
        {
            "updated_utc": recovered_utc,
            "plan_sha256": plan["plan_sha256"],
            "planned_jobs": plan["job_count"],
            "latest_rows": len(rows),
            "terminal_rows": sum(
                row.get("status") in TERMINAL_STATUSES for row in rows
            ),
            "error_rows": sum(row.get("status") == "ERROR" for row in rows),
            "interrupted": False,
            "normalized_result_recovery": True,
        },
    )
    errors, report = validate_campaign(
        output,
        allow_dirty=allow_dirty,
        allow_incomplete=True,
        allow_environment_drift=allow_environment_drift,
    )
    _write_json(output / "validation_report.json", report)
    if errors:
        raise ValueError(
            "recovered campaign failed validation: " + "; ".join(errors[:10])
        )
    return {
        "existing_records": len(existing_latest),
        "recovered_records": len(recovered_records),
        "assigned_jobs": len(assigned_keys),
        "normalized_sha256": normalized_sha256,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recover missing raw journal records from a complete normalized CSV. "
            "Every reconstructed record is marked with its source digest."
        )
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-environment-drift", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = recover_results(
            args.output,
            allow_dirty=args.allow_dirty,
            allow_environment_drift=args.allow_environment_drift,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(
        "recovery_state "
        f"existing={report['existing_records']} "
        f"recovered={report['recovered_records']} "
        f"assigned={report['assigned_jobs']} "
        f"source_sha256={report['normalized_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
