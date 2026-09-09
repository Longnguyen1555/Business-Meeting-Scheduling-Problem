from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
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


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _compatible_environment_errors(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> list[str]:
    exact_fields = (
        "plan_sha256",
        "git_commit",
        "git_dirty",
        "config_sha256",
        "requirements_sha256",
        "manifest_sha256",
        "uwrmaxsat_sha256",
        "required_machine",
        "python_version",
        "kernel_release",
        "cpu_model",
        "physical_cpu_cores",
        "logical_cpu_cores",
        "execution_shard_policy",
    )
    return [
        field
        for field in exact_fields
        if candidate.get(field) != reference.get(field)
    ]


def merge_shards(
    inputs: list[Path],
    output: Path,
    *,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Merge complete, disjoint execution shards of one frozen campaign."""

    resolved_inputs = [path.resolve() for path in inputs]
    output = output.resolve()
    if len(resolved_inputs) < 2:
        raise ValueError("at least two shard directories are required")
    if len(set(resolved_inputs)) != len(resolved_inputs):
        raise ValueError("shard directories must be distinct")
    if output in resolved_inputs:
        raise ValueError("output directory cannot also be an input shard")
    for path in resolved_inputs:
        try:
            output.relative_to(path)
        except ValueError:
            continue
        raise ValueError("output directory cannot be inside an input shard")
    if output.exists() and not output.is_dir():
        raise ValueError(f"output path is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(
            f"refusing to overwrite non-empty output directory: {output}"
        )

    plans = [_read_json(path / "plan.json") for path in resolved_inputs]
    reference_plan = plans[0]
    for index, plan in enumerate(plans[1:], start=2):
        if canonical_json(plan) != canonical_json(reference_plan):
            raise ValueError(f"shard {index} has a different campaign plan")

    environments = [
        _read_json(path / "environment.json") for path in resolved_inputs
    ]
    reference_environment = environments[0]
    shard_count = int(reference_environment.get("execution_shard_count", 1))
    if shard_count <= 1:
        raise ValueError("inputs are not declared as a multi-shard execution")
    if (
        reference_environment.get("execution_shard_policy")
        != EXECUTION_SHARD_POLICY
    ):
        raise ValueError("inputs use an unknown execution shard policy")
    shard_assignments = execution_shard_assignments(reference_plan, shard_count)

    assigned: set[int] = set()
    records: list[dict[str, Any]] = []
    for position, (path, environment) in enumerate(
        zip(resolved_inputs, environments), start=1
    ):
        source_errors, _ = validate_campaign(
            path,
            allow_dirty=allow_dirty,
            allow_incomplete=True,
        )
        if source_errors:
            raise ValueError(
                f"shard {position} failed validation: "
                + "; ".join(source_errors[:5])
            )
        mismatches = _compatible_environment_errors(
            reference_environment, environment
        )
        if mismatches:
            raise ValueError(
                f"shard {position} has incompatible environment fields: "
                + ", ".join(mismatches)
            )
        if int(environment.get("execution_shard_count", 1)) != shard_count:
            raise ValueError(f"shard {position} has a different shard count")
        indices = tuple(
            int(value)
            for value in environment.get("execution_shard_indices", [])
        )
        if not indices or any(
            value < 0 or value >= shard_count for value in indices
        ):
            raise ValueError(f"shard {position} declares invalid shard indices")
        overlap = assigned.intersection(indices)
        if overlap:
            raise ValueError(
                f"execution shard indices overlap: {sorted(overlap)}"
            )
        assigned.update(indices)

        source_records = _read_jsonl(path / "raw" / "results.jsonl")
        source_latest = latest_attempts(source_records)
        records.extend(source_records)

        expected_keys = {
            job["run_key"]
            for job in reference_plan["jobs"]
            if shard_assignments[job["run_key"]] in indices
        }
        observed_keys = set(source_latest)
        if observed_keys != expected_keys:
            raise ValueError(
                f"shard {position} is incomplete or contains unassigned jobs: "
                f"expected {len(expected_keys)}, observed {len(observed_keys)}"
            )
        nonterminal = sorted(
            run_key
            for run_key, record in source_latest.items()
            if record.get("row", {}).get("status") not in TERMINAL_STATUSES
        )
        if nonterminal:
            raise ValueError(
                f"shard {position} has {len(nonterminal)} non-terminal jobs"
            )
        for run_key, record in source_latest.items():
            row = record.get("row", {})
            expected_index = shard_assignments[run_key]
            if int(row.get("execution_shard_index", -1)) != expected_index:
                raise ValueError(
                    f"{run_key}: recorded execution shard is inconsistent"
                )

    expected_indices = set(range(shard_count))
    if assigned != expected_indices:
        raise ValueError(
            "input directories do not cover every execution shard: "
            f"covered={sorted(assigned)}, expected={sorted(expected_indices)}"
        )

    # Disjoint shard assignments imply disjoint run keys, including retries.
    combined_latest = latest_attempts(records)
    if len(combined_latest) != int(reference_plan["job_count"]):
        raise ValueError("combined results do not cover the full campaign")

    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "plan.json", reference_plan)

    merged_environment = dict(reference_environment)
    merged_environment.update(
        {
            "created_utc": _utc_now(),
            "hostname": "MULTI_VM_MERGE",
            "boot_id": "",
            "runner_command": "Merge_Journal_Shards.py",
            "execution_shard_indices": sorted(assigned),
            "merged_from_shards": [
                {
                    "path": str(path),
                    "hostname": environment.get("hostname", ""),
                    "boot_id": environment.get("boot_id", ""),
                    "execution_shard_indices": environment.get(
                        "execution_shard_indices", []
                    ),
                }
                for path, environment in zip(resolved_inputs, environments)
            ],
        }
    )
    _write_json(output / "environment.json", merged_environment)
    environment_dir = output / "environments"
    for position, environment in enumerate(environments, start=1):
        _write_json(environment_dir / f"source-{position:02d}.json", environment)

    run_order = {
        job["run_key"]: int(job["run_order"])
        for job in reference_plan["jobs"]
    }
    records.sort(
        key=lambda record: (
            run_order[record["run_key"]],
            int(record["attempt"]),
        )
    )
    raw_path = output / "raw" / "results.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    for position, path in enumerate(resolved_inputs, start=1):
        source_logs = path / "logs"
        if source_logs.is_dir():
            shutil.copytree(
                source_logs,
                output / "logs" / f"source-{position:02d}",
            )

    rows = materialize_results(output)
    _write_json(
        output / "campaign_status.json",
        {
            "updated_utc": _utc_now(),
            "plan_sha256": reference_plan["plan_sha256"],
            "planned_jobs": reference_plan["job_count"],
            "latest_rows": len(rows),
            "terminal_rows": sum(
                row.get("status") in TERMINAL_STATUSES for row in rows
            ),
            "error_rows": sum(row.get("status") == "ERROR" for row in rows),
            "merged_sources": len(resolved_inputs),
        },
    )
    errors, report = validate_campaign(output, allow_dirty=allow_dirty)
    _write_json(output / "validation_report.json", report)
    if errors:
        raise ValueError(
            "merged campaign failed strict validation: " + "; ".join(errors[:5])
        )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge complete, disjoint shards of one journal campaign."
    )
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="development only; production shard environments must be clean",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = merge_shards(
            [Path(value) for value in args.input],
            Path(args.output),
            allow_dirty=args.allow_dirty,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(
        f"merged={report['latest_attempts']}/{report['planned_jobs']} "
        f"valid={report['valid']} output={Path(args.output).resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
