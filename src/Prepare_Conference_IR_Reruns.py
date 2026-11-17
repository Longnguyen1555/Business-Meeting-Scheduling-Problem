"""Build a validated archive from the two checked-in Conference IR reruns."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from Dataset_Manifest import file_sha256
from Journal_Experiment import (
    EXECUTION_SHARD_POLICY,
    PROJECT_ROOT,
    _write_json,
    build_plan,
    canonical_json,
    materialize_results,
    read_config,
    resolve_datasets,
    sha256_text,
)
from Prepare_Conference_Reuse import (
    FORMULA_IDENTITY_FIELDS,
    _archive_row,
    _formula_signature,
    _primary_value,
    _proven_value,
    _read_csv,
)
from Validate_Journal_Run import validate_campaign


ARCHIVE_CONFIG = (
    PROJECT_ROOT / "journal_configs" / "conference_ir_reruns.json"
)
ARCHIVE_SCHEMA_VERSION = 2
SOURCE_ROOT = PROJECT_ROOT / "output" / "conference_ir"
DEFAULT_COMPACT_CSVS = (
    PROJECT_ROOT
    / "outputs/journal/compact-objectives/shard-0-of-2/normalized/detailed.csv",
    PROJECT_ROOT
    / "outputs/journal/compact-objectives/shard-1-of-2/normalized/detailed.csv",
)
DEFAULT_STRESS_REFERENCE = (
    PROJECT_ROOT
    / "outputs/journal/compact-ablation-stress-sessions/a06/normalized/detailed.csv"
)
SOURCE_STEMS = (
    "table03",
    "table06",
    "table07",
    "table08",
    "stress",
    "stress_high",
)
EXPECTED_PER_STEM = {
    "table03": 20,
    "table06": 26,
    "table07": 40,
    "table08": 40,
    "stress": 60,
    "stress_high": 40,
}
EXPECTED_STATUS_BY_DATASET = {
    "All-126": {"OPTIMAL": 89, "UNSAT": 37},
    "Stress": {"OPTIMAL": 60},
    "Stress-high": {"OPTIMAL": 40},
}
CONFIGURATION_METADATA_FIELDS = (
    "precedence_encoding",
    "precedence_graph",
    "precedence_configuration",
    "factor_p",
    "factor_g",
)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        if not fieldnames:
            return
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def index_unique(
    rows: Iterable[dict[str, Any]], *, label: str, expected: int
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        content_id = str(row.get("instance_content_id", "")).strip()
        if not content_id:
            raise ValueError(f"{label} contains a row without instance_content_id")
        if content_id in indexed:
            raise ValueError(f"{label} duplicates content {content_id}")
        indexed[content_id] = row
    if len(indexed) != expected:
        raise ValueError(f"{label} must contain {expected} contents, got {len(indexed)}")
    return indexed


def _dataset_for_content(
    content_id: str, dataset_contents: dict[str, set[str]]
) -> str:
    matches = [name for name, contents in dataset_contents.items() if content_id in contents]
    if len(matches) != 1:
        raise ValueError(
            f"content {content_id} belongs to {len(matches)} frozen datasets"
        )
    return matches[0]


def _normalized_objective(row: dict[str, Any]) -> tuple[str, str, str]:
    if str(row.get("status", "")) != "OPTIMAL":
        return ("", "", "")
    return (
        _primary_value(row),
        _proven_value(row),
        str(row.get("objective_vector", "")).strip(),
    )


def audit_equivalence(
    source: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
    *,
    label: str,
    permit_empty_precedence_metadata: bool = True,
) -> dict[str, Any]:
    """Require exact results/formulas, allowing inert precedence metadata only."""

    if source.keys() != reference.keys():
        missing = sorted(reference.keys() - source.keys())
        extra = sorted(source.keys() - reference.keys())
        raise ValueError(
            f"{label} content mismatch: missing={missing[:3]}, extra={extra[:3]}"
        )
    mismatches: list[str] = []
    accepted_metadata_differences = 0
    for content_id in sorted(reference):
        left = source[content_id]
        right = reference[content_id]
        for field in ("instance_sha256", "status", "sat_result"):
            if str(left.get(field, "")) != str(right.get(field, "")):
                mismatches.append(f"{content_id}:{field}")
        if _normalized_objective(left) != _normalized_objective(right):
            mismatches.append(f"{content_id}:objective_vector")
        for field in FORMULA_IDENTITY_FIELDS:
            if str(left.get(field, "")) != str(right.get(field, "")):
                mismatches.append(f"{content_id}:{field}")

        metadata_differs = any(
            str(left.get(field, "")) != str(right.get(field, ""))
            for field in CONFIGURATION_METADATA_FIELDS
        )
        if metadata_differs:
            empty_precedence = (
                str(left.get("precedence_relation_edges", "")) == "0"
                and str(right.get("precedence_relation_edges", "")) == "0"
            )
            same_formula = _formula_signature(left) == _formula_signature(right)
            if permit_empty_precedence_metadata and empty_precedence and same_formula:
                accepted_metadata_differences += 1
            else:
                mismatches.append(f"{content_id}:precedence_metadata")
    if mismatches:
        raise ValueError(
            f"{label} failed formula/result identity: " + ", ".join(mismatches[:10])
        )
    return {
        "contents": len(source),
        "formula_fields_checked": len(FORMULA_IDENTITY_FIELDS),
        "formula_fields": list(FORMULA_IDENTITY_FIELDS),
        "status_counts": dict(Counter(row["status"] for row in source.values())),
        "accepted_empty_precedence_metadata_differences": (
            accepted_metadata_differences
        ),
        "identity_mismatches": 0,
    }


def _summary_display(row: dict[str, Any]) -> str:
    if str(row.get("status", "")) == "UNSAT":
        return "UNSAT"
    if str(row.get("status", "")) != "OPTIMAL":
        return str(row.get("status", ""))
    return f"{float(row['runtime_seconds']):.1f} {_primary_value(row)}"


def _read_source_run(
    run_number: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_dir = SOURCE_ROOT / f"run{run_number}"
    detailed_rows: list[dict[str, Any]] = []
    files: dict[str, dict[str, Any]] = {}
    for stem in SOURCE_STEMS:
        detailed_path = run_dir / f"{stem}_detailed.csv"
        summary_path = run_dir / f"{stem}.csv"
        detailed = _read_csv(detailed_path)
        summary = _read_csv(summary_path)
        expected = EXPECTED_PER_STEM[stem]
        if len(detailed) != expected or len(summary) != expected:
            raise ValueError(
                f"run{run_number}/{stem} expected {expected} rows; "
                f"detailed={len(detailed)}, summary={len(summary)}"
            )
        detailed_by_instance = {str(row["instance"]): row for row in detailed}
        summary_by_instance = {str(row["instance"]): row for row in summary}
        if len(detailed_by_instance) != expected or len(summary_by_instance) != expected:
            raise ValueError(f"run{run_number}/{stem} duplicates an instance")
        if detailed_by_instance.keys() != summary_by_instance.keys():
            raise ValueError(f"run{run_number}/{stem} summary instance mismatch")
        for instance, source_row in detailed_by_instance.items():
            summary_row = summary_by_instance[instance]
            if summary_row.get("imp12+", "") != _summary_display(source_row):
                raise ValueError(
                    f"run{run_number}/{stem}/{instance} summary value mismatch"
                )
            copied = dict(source_row)
            copied["_archive_source_row_sha256"] = sha256_text(
                canonical_json(source_row)
            )
            copied["_archive_source_path"] = detailed_path.relative_to(
                PROJECT_ROOT
            ).as_posix()
            copied["_archive_source_file_sha256"] = file_sha256(detailed_path)
            copied["_archive_source_summary_path"] = summary_path.relative_to(
                PROJECT_ROOT
            ).as_posix()
            copied["_archive_source_summary_sha256"] = file_sha256(summary_path)
            detailed_rows.append(copied)
        files[stem] = {
            "detailed_path": detailed_path.relative_to(PROJECT_ROOT).as_posix(),
            "detailed_sha256": file_sha256(detailed_path),
            "summary_path": summary_path.relative_to(PROJECT_ROOT).as_posix(),
            "summary_sha256": file_sha256(summary_path),
            "rows": expected,
        }
    return detailed_rows, {"run": run_number, "files": files}


def _reference_rows(
    compact_csvs: Iterable[Path], stress_reference: Path
) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in compact_csvs:
        rows.extend(
            row
            for row in _read_csv(path)
            if row.get("planned_configuration_id") == "ir_reference"
            and row.get("objective_mode") == "ir"
        )
    rows.extend(
        row
        for row in _read_csv(stress_reference)
        if row.get("planned_configuration_id") == "ir_reference"
        and row.get("objective_mode") == "ir"
    )
    return index_unique(rows, label="current Conference IR references", expected=226)


def _source_row_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if row.get("status") not in {"OPTIMAL", "UNSAT"}:
        errors.append(f"status={row.get('status', '')}")
    for field in ("error_type", "error_message", "validation_errors"):
        if str(row.get(field, "")).strip():
            errors.append(field)
    if row.get("objective_mode") != "ir":
        errors.append("objective_mode")
    if row.get("compact_encoding") != "reference":
        errors.append("compact_encoding")
    return errors


def _runtime_variation(
    run1: dict[str, dict[str, Any]], run2: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    symmetric_ratios: list[float] = []
    for content_id in sorted(run1):
        first = float(run1[content_id]["runtime_seconds"])
        second = float(run2[content_id]["runtime_seconds"])
        ratio = first / second
        symmetric = max(ratio, 1.0 / ratio)
        symmetric_ratios.append(symmetric)
        rows.append(
            {
                "instance_content_id": content_id,
                "instance": run1[content_id]["instance"],
                "run1_seconds": first,
                "run2_seconds": second,
                "run1_over_run2": ratio,
                "larger_over_smaller": symmetric,
            }
        )
    report = {
        "paired_instances": len(rows),
        "median_run1_over_run2": statistics.median(
            row["run1_over_run2"] for row in rows
        ),
        "geometric_mean_run1_over_run2": math.exp(
            statistics.fmean(math.log(row["run1_over_run2"]) for row in rows)
        ),
        "maximum_larger_over_smaller": max(symmetric_ratios),
        "ratios_above_1_5": sum(value > 1.5 for value in symmetric_ratios),
    }
    return report, rows


def _legacy_runtime_diagnostic(
    reference: dict[str, dict[str, Any]],
    reruns: Sequence[dict[str, dict[str, Any]]],
    dataset_contents: dict[str, set[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detail: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for dataset, content_ids in dataset_contents.items():
        ratios: list[float] = []
        for content_id in sorted(content_ids):
            legacy = float(reference[content_id]["runtime_seconds"])
            rerun_median = statistics.median(
                float(run[content_id]["runtime_seconds"]) for run in reruns
            )
            ratio = legacy / rerun_median
            ratios.append(ratio)
            detail.append(
                {
                    "dataset": dataset,
                    "instance_content_id": content_id,
                    "legacy_seconds": legacy,
                    "rerun_median_seconds": rerun_median,
                    "legacy_over_rerun_median": ratio,
                }
            )
        report[dataset] = {
            "instances": len(ratios),
            "median_legacy_over_rerun": statistics.median(ratios),
            "geometric_mean_legacy_over_rerun": math.exp(
                statistics.fmean(math.log(value) for value in ratios)
            ),
            "maximum_legacy_over_rerun": max(ratios),
        }
    return report, detail


def prepare_archive(
    output: Path,
    *,
    config_path: Path = ARCHIVE_CONFIG,
    compact_csvs: Iterable[Path] = DEFAULT_COMPACT_CSVS,
    stress_reference: Path = DEFAULT_STRESS_REFERENCE,
) -> dict[str, Any]:
    output = output.resolve()
    compact_csvs = tuple(path.resolve() for path in compact_csvs)
    stress_reference = stress_reference.resolve()

    config = read_config(config_path.resolve())
    datasets = resolve_datasets(config)
    dataset_contents = {
        "All-126": {instance.content_id for instance in datasets["official_all_126"]},
        "Stress": {
            instance.content_id for instance in datasets["precedence_stress_60"]
        },
        "Stress-high": {
            instance.content_id
            for instance in datasets["precedence_stress_high_40"]
        },
    }
    content_by_sha = {
        instance.sha256: instance.content_id
        for instances in datasets.values()
        for instance in instances
    }
    if len(content_by_sha) != 226:
        raise ValueError("the three frozen datasets do not contain 226 unique hashes")
    run_rows: list[dict[str, dict[str, Any]]] = []
    source_provenance: list[dict[str, Any]] = []
    for run_number in (1, 2):
        rows, provenance = _read_source_run(run_number)
        for row in rows:
            source_content_id = str(row.get("instance_content_id", ""))
            instance_sha256 = str(row.get("instance_sha256", ""))
            if instance_sha256 not in content_by_sha:
                raise ValueError(
                    f"run{run_number} contains unknown instance SHA-256 "
                    f"{instance_sha256}"
                )
            row["_archive_source_instance_content_id"] = source_content_id
            row["instance_content_id"] = content_by_sha[instance_sha256]
        indexed = index_unique(
            rows, label=f"Conference IR run{run_number}", expected=226
        )
        invalid = {
            content_id: _source_row_errors(row)
            for content_id, row in indexed.items()
            if _source_row_errors(row)
        }
        if invalid:
            raise ValueError(
                f"Conference IR run{run_number} has invalid rows: "
                + ", ".join(list(invalid)[:5])
            )
        for dataset, contents in dataset_contents.items():
            counts = Counter(indexed[key]["status"] for key in contents)
            if dict(counts) != EXPECTED_STATUS_BY_DATASET[dataset]:
                raise ValueError(
                    f"run{run_number}/{dataset} status counts {dict(counts)}; "
                    f"expected {EXPECTED_STATUS_BY_DATASET[dataset]}"
                )
        run_rows.append(indexed)
        source_provenance.append(provenance)

    raw_reference = _reference_rows(compact_csvs, stress_reference)
    canonical_reference_rows: list[dict[str, Any]] = []
    for row in raw_reference.values():
        copied = dict(row)
        instance_sha256 = str(copied.get("instance_sha256", ""))
        if instance_sha256 not in content_by_sha:
            raise ValueError(
                f"reference contains unknown instance SHA-256 {instance_sha256}"
            )
        copied["instance_content_id"] = content_by_sha[instance_sha256]
        canonical_reference_rows.append(copied)
    reference = index_unique(
        canonical_reference_rows,
        label="canonical current Conference IR references",
        expected=226,
    )
    run_audits = [
        audit_equivalence(run, reference, label=f"Conference IR run{position}")
        for position, run in enumerate(run_rows, start=1)
    ]
    paired_audit = audit_equivalence(
        run_rows[0],
        run_rows[1],
        label="Conference IR run1 versus run2",
        permit_empty_precedence_metadata=False,
    )
    runtime_report, runtime_rows = _runtime_variation(run_rows[0], run_rows[1])
    legacy_report, legacy_rows = _legacy_runtime_diagnostic(
        reference, run_rows, dataset_contents
    )

    plan = build_plan(config, datasets)
    unhashed_plan = dict(plan)
    unhashed_plan.pop("plan_sha256", None)
    unhashed_plan["archive_provenance"] = {
        "source_runs": source_provenance,
        "reference_csv_sha256": {
            path.relative_to(PROJECT_ROOT).as_posix(): file_sha256(path)
            for path in (*compact_csvs, stress_reference)
        },
        "source_repetition_mapping": {"run1": 1, "run2": 2},
        "target_repetition_mapping": {"run1": 2, "run2": 3},
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "equivalence_policy": (
            "exact instance, status, SAT result, objective vector, and formula "
            "signature; inert precedence metadata may differ only when both "
            "rows have zero precedence relations"
        ),
    }
    unhashed_plan["plan_sha256"] = sha256_text(canonical_json(unhashed_plan))
    plan = unhashed_plan

    if output.exists() and any(output.iterdir()):
        errors, validation_report = validate_campaign(
            output, allow_dirty=True, allow_environment_drift=True
        )
        if errors:
            raise ValueError(
                f"refusing to overwrite invalid non-empty archive {output}: "
                + "; ".join(errors[:5])
            )
        existing_plan = json.loads(
            (output / "plan.json").read_text(encoding="utf-8")
        )
        if existing_plan.get("plan_sha256") == plan["plan_sha256"]:
            return validation_report
        raise ValueError(
            "the existing Conference IR rerun archive is valid but stale; "
            "choose a new output path"
        )

    jobs = {
        (int(job["repetition"]), str(job["instance_content_id"])): job
        for job in plan["jobs"]
    }
    records: list[dict[str, Any]] = []
    for source_repetition, source_rows in enumerate(run_rows, start=1):
        for content_id, source in source_rows.items():
            job = jobs[(source_repetition, content_id)]
            row = _archive_row(
                source,
                reference[content_id],
                job,
                plan,
                historical_role=(
                    f"conference_ir_source_repetition_{source_repetition}"
                ),
            )
            row.update(
                {
                    "archive_source_path": source["_archive_source_path"],
                    "archive_source_file_sha256": source[
                        "_archive_source_file_sha256"
                    ],
                    "archive_source_summary_path": source[
                        "_archive_source_summary_path"
                    ],
                    "archive_source_summary_sha256": source[
                        "_archive_source_summary_sha256"
                    ],
                    "archive_source_row_sha256": source[
                        "_archive_source_row_sha256"
                    ],
                    "archive_source_repetition": source_repetition,
                    "archive_target_repetition": source_repetition + 1,
                    "archive_source_instance_content_id": source[
                        "_archive_source_instance_content_id"
                    ],
                    "archive_dataset": _dataset_for_content(
                        content_id, dataset_contents
                    ),
                }
            )
            for field in tuple(row):
                if field.startswith("_archive_"):
                    row.pop(field)
            records.append(
                {
                    "run_key": job["run_key"],
                    "attempt": 1,
                    "completed_utc": str(
                        source.get("run_started_utc", "2026-10-23T00:00:00+00:00")
                    ),
                    "row": row,
                }
            )
    records.sort(key=lambda record: int(record["row"]["run_order"]))

    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "plan.json", plan)
    manifest_hashes = {
        str(dataset["manifest"]): file_sha256(PROJECT_ROOT / dataset["manifest"])
        for dataset in config["datasets"]
    }
    solver_hashes = sorted(
        {
            str(row.get("solver_binary_sha256", ""))
            for source_rows in run_rows
            for row in source_rows.values()
        }
    )
    environment = {
        "created_utc": "2026-10-24T00:00:00+07:00",
        "campaign_id": plan["campaign_id"],
        "config_path": config_path.resolve().relative_to(PROJECT_ROOT).as_posix(),
        "config_sha256": file_sha256(config_path.resolve()),
        "plan_sha256": plan["plan_sha256"],
        "git_commit": "MULTIPLE",
        "git_dirty": True,
        "requirements_sha256": "MULTIPLE",
        "manifest_sha256": manifest_hashes,
        "uwrmaxsat_binary": "MULTIPLE",
        "uwrmaxsat_sha256": "MULTIPLE",
        "python_version": "MULTIPLE",
        "kernel_release": "MULTIPLE",
        "platform": "MULTIPLE",
        "hostname": "CONFERENCE_IR_RERUN_ARCHIVE",
        "boot_id": "",
        "cpu_model": "Intel(R) Xeon(R) Platinum 8581C CPU @ 2.30GHz",
        "physical_cpu_cores": 4,
        "logical_cpu_cores": 8,
        "system_memory_mb": 15988.0,
        "swap_memory_mb": 0,
        "required_machine": plan["required_machine"],
        "execution_shard_count": 1,
        "execution_shard_indices": [0],
        "execution_shard_policy": EXECUTION_SHARD_POLICY,
        "runner_command": "Prepare_Conference_IR_Reruns.py",
        "environment_drift_accepted": True,
        "archive_only": True,
        "source_solver_binary_sha256": solver_hashes,
    }
    _write_json(output / "environment.json", environment)
    raw_path = output / "raw" / "results.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    normalized_rows = materialize_results(output)
    _write_json(
        output / "campaign_status.json",
        {
            "planned_jobs": len(plan["jobs"]),
            "latest_rows": len(normalized_rows),
            "terminal_rows": len(normalized_rows),
            "error_rows": 0,
            "archive_only": True,
            "plan_sha256": plan["plan_sha256"],
        },
    )

    per_run_summary = []
    for position, source_rows in enumerate(run_rows, start=1):
        for dataset, contents in dataset_contents.items():
            counts = Counter(source_rows[key]["status"] for key in contents)
            per_run_summary.append(
                {
                    "source_repetition": position,
                    "target_repetition": position + 1,
                    "dataset": dataset,
                    "instances": len(contents),
                    "OPTIMAL": counts.get("OPTIMAL", 0),
                    "UNSAT": counts.get("UNSAT", 0),
                    "TIMEOUT": counts.get("TIMEOUT", 0),
                    "ERROR": counts.get("ERROR", 0),
                    "median_runtime_seconds": statistics.median(
                        float(source_rows[key]["runtime_seconds"])
                        for key in contents
                    ),
                }
            )
    _write_csv(output / "per_run_summary.csv", per_run_summary)
    _write_json(
        output / "provenance_report.json",
        {
            "source_runs": source_provenance,
            "solver_binary_sha256_values": solver_hashes,
            "solver_hashes_are_descriptive": True,
            "common_gcp_environment_assumed": True,
        },
    )
    _write_json(output / "paired_runtime_variation_report.json", runtime_report)
    _write_csv(output / "paired_runtime_variation.csv", runtime_rows)
    _write_json(output / "legacy_vs_rerun_diagnostic_report.json", legacy_report)
    _write_csv(output / "legacy_vs_rerun_diagnostic.csv", legacy_rows)
    equivalence_report = {
        "valid": True,
        "archive_campaign_id": plan["campaign_id"],
        "archive_plan_sha256": plan["plan_sha256"],
        "source_run_audits": run_audits,
        "paired_run_audit": paired_audit,
        "runtime_variation": runtime_report,
        "status_expectations": EXPECTED_STATUS_BY_DATASET,
        "source_provenance": source_provenance,
    }
    _write_json(output / "equivalence_report.json", equivalence_report)

    errors, validation_report = validate_campaign(
        output, allow_dirty=True, allow_environment_drift=True
    )
    _write_json(output / "validation_report.json", validation_report)
    if errors:
        raise ValueError(
            "prepared Conference IR rerun archive failed validation: "
            + "; ".join(errors[:10])
        )
    return validation_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the audited 452-row Conference IR rerun archive."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=ARCHIVE_CONFIG)
    parser.add_argument(
        "--compact-reference-csv", action="append", type=Path, default=[]
    )
    parser.add_argument(
        "--stress-reference-csv", type=Path, default=DEFAULT_STRESS_REFERENCE
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = prepare_archive(
        args.output,
        config_path=args.config,
        compact_csvs=args.compact_reference_csv or DEFAULT_COMPACT_CSVS,
        stress_reference=args.stress_reference_csv,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
