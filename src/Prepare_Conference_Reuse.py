from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

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
from Validate_Journal_Run import validate_campaign


ARCHIVE_CONFIG = PROJECT_ROOT / "journal_configs" / "conference_historical_reuse.json"
DEFAULT_COMPACT_CSVS = (
    PROJECT_ROOT
    / "outputs/journal/compact-objectives/shard-0-of-2/normalized/detailed.csv",
    PROJECT_ROOT
    / "outputs/journal/compact-objectives/shard-1-of-2/normalized/detailed.csv",
)
DEFAULT_BG_LOCAL_CSV = (
    PROJECT_ROOT
    / "outputs/journal/all126-first/shards-0-1-2-of-4/normalized/detailed.csv"
)
DEFAULT_BG_SHARD3_REF = "origin/IR-IM-IS"
DEFAULT_BG_SHARD3_PATH = (
    "outputs/journal/all126-first/shard-3-of-4/normalized/detailed.csv"
)
DEFAULT_IR_CSVS = tuple(
    PROJECT_ROOT
    / "output/ic12p-gcp-ic12p-20260723T023309Z-normalized/main"
    / name
    for name in (
        "data_table03_origin_detailed.csv",
        "data_table06_forb_detailed.csv",
        "data_table07_fixed_detailed.csv",
        "data_table08_prec_detailed.csv",
    )
)

FORMULA_IDENTITY_FIELDS = (
    "n_vars",
    "n_hard_clauses",
    "n_soft_clauses",
    "n_total_clauses",
    "n_primary_variables",
    "n_auxiliary_variables",
    "n_total_literals",
    "max_hard_clause_length",
    "n_hard_literals",
    "n_soft_literals",
    "max_soft_clause_length",
    "n_unit_hard_clauses",
    "n_binary_hard_clauses",
    "n_ternary_hard_clauses",
    "n_long_hard_clauses",
    "soft_clause_weight",
    "soft_weight_sum",
    "n_objective_lits",
    "optimizer_added_variables_peak",
    "optimizer_added_clauses_peak",
    "optimizer_added_literals_peak",
    "optimizer_added_clauses_cumulative",
    "full_schedule_candidates",
    "unary_eligible_schedule_candidates",
    "reduced_schedule_candidates",
    "active_schedule_candidates",
    "precedence_direct_edges",
    "precedence_closure_edges",
    "precedence_relation_edges",
    "precedence_pairwise_clauses",
    "precedence_sparse_link_clauses",
    "initial_schedule_candidates",
    "unary_removed_schedule_candidates",
    "preprocessing_removed_schedule_candidates",
    "removed_schedule_candidates",
)

CANONICAL_CONFIGURATION_FIELDS = (
    "configuration_label",
    "factor_m",
    "factor_f",
    "factor_p",
    "factor_g",
    "factor_b",
    "factor_o",
    "factor_s",
    "factor_i",
    "configuration_id",
    "configuration_key",
    "domain_mode",
    "domain_filter_graph",
    "precedence_encoding",
    "precedence_graph",
    "optimization_engine",
    "solver_backend",
    "encoding_variant",
    "idle_encoding",
    "objective",
    "objective_code",
    "implied_constraints_code",
    "compact_encoding",
    "collision_amo",
    "participant_idle_cap_rule",
    "participant_idle_cap_alpha",
    "precedence_configuration",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _read_csvs(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(_read_csv(path))
    return rows


def _git_file(ref: str, path: str) -> tuple[str, str, str]:
    spec = f"{ref}:{path}"
    try:
        text = subprocess.check_output(
            ["git", "show", spec], cwd=PROJECT_ROOT, text=True
        )
        blob = subprocess.check_output(
            ["git", "rev-parse", spec], cwd=PROJECT_ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"cannot read {spec}; fetch the {ref!r} remote branch first"
        ) from exc
    return text, blob, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rows_by_content(
    rows: Iterable[dict[str, Any]], *, label: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        content_id = str(row.get("instance_content_id", ""))
        if not content_id:
            raise ValueError(f"{label} contains a row without instance_content_id")
        if content_id in indexed:
            raise ValueError(f"{label} duplicates content {content_id}")
        indexed[content_id] = row
    if len(indexed) != 126:
        raise ValueError(f"{label} must contain 126 contents, got {len(indexed)}")
    return indexed


def _reference_rows(
    compact_csvs: Iterable[Path], objective_mode: str
) -> dict[str, dict[str, Any]]:
    selected = [
        row
        for row in _read_csvs(compact_csvs)
        if row.get("objective_mode") == objective_mode
        and row.get("compact_encoding") == "reference"
    ]
    return _rows_by_content(selected, label=f"current {objective_mode} reference")


def _historical_bg_rows(
    local_csv: Path, shard3_ref: str, shard3_path: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    remote_text, remote_blob, remote_sha256 = _git_file(
        shard3_ref, shard3_path
    )
    rows = _read_csv(local_csv)
    rows.extend(csv.DictReader(io.StringIO(remote_text)))
    selected = [
        dict(row)
        for row in rows
        if row.get("planned_configuration_id")
        == "compact_reduced_bg_reference"
        and row.get("objective_mode") == "bg_d2"
        and row.get("compact_encoding") == "reference"
    ]
    provenance = {
        "local_csv": str(local_csv.relative_to(PROJECT_ROOT)),
        "local_csv_sha256": file_sha256(local_csv),
        "shard3_git_ref": shard3_ref,
        "shard3_git_path": shard3_path,
        "shard3_git_blob": remote_blob,
        "shard3_sha256": remote_sha256,
    }
    return _rows_by_content(selected, label="historical BG-d2 reference"), provenance


def _historical_ir_rows(
    paths: Iterable[Path],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    selected = []
    source_hashes = {}
    for path in paths:
        source_hashes[str(path.relative_to(PROJECT_ROOT))] = file_sha256(path)
        for row in _read_csv(path):
            if row.get("factor_m") != "Reduced" or row.get("factor_s") != "UWrMaxSAT":
                continue
            if row.get("instance_family") == "precedence":
                expected = ("SparseSuffix", "DistanceClosure-E*")
            else:
                expected = ("Pairwise", "Direct-E")
            if (row.get("factor_p"), row.get("factor_g")) != expected:
                continue
            if row.get("objective") != "internal_idle_slot_range_pstar":
                continue
            selected.append(row)
    return (
        _rows_by_content(selected, label="published conference IR"),
        {"csv_sha256": source_hashes},
    )


def _primary_value(row: dict[str, Any]) -> str:
    for field in (
        "primary_objective_value",
        "objective_value",
        "best_value",
        "idle_range_pstar",
    ):
        value = str(row.get(field, "")).strip()
        if value:
            return value
    return ""


def _proven_value(row: dict[str, Any]) -> str:
    for field in (
        "proven_objective_vector",
        "proven_optimum",
        "primary_objective_value",
        "objective_value",
    ):
        value = str(row.get(field, "")).strip()
        if value:
            return value
    return ""


def _formula_signature(row: dict[str, Any]) -> dict[str, str]:
    return {field: str(row.get(field, "")) for field in FORMULA_IDENTITY_FIELDS}


def _audit_equivalence(
    historical: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
    *,
    label: str,
) -> dict[str, Any]:
    if historical.keys() != reference.keys():
        missing = sorted(reference.keys() - historical.keys())
        extra = sorted(historical.keys() - reference.keys())
        raise ValueError(
            f"{label} content mismatch: missing={missing[:3]}, extra={extra[:3]}"
        )
    mismatches: list[str] = []
    for content_id in sorted(reference):
        source = historical[content_id]
        target = reference[content_id]
        for field in ("instance_sha256", "status", "sat_result"):
            if str(source.get(field, "")) != str(target.get(field, "")):
                mismatches.append(f"{content_id}:{field}")
        if source.get("status") == "OPTIMAL":
            if _primary_value(source) != _primary_value(target):
                mismatches.append(f"{content_id}:primary_objective")
            if _proven_value(source) != _proven_value(target):
                mismatches.append(f"{content_id}:proven_objective")
        source_signature = _formula_signature(source)
        target_signature = _formula_signature(target)
        for field in FORMULA_IDENTITY_FIELDS:
            if source_signature[field] != target_signature[field]:
                mismatches.append(f"{content_id}:{field}")
    if mismatches:
        raise ValueError(
            f"{label} failed formula/result identity: " + ", ".join(mismatches[:10])
        )
    return {
        "contents": 126,
        "formula_fields": list(FORMULA_IDENTITY_FIELDS),
        "formula_fields_checked": len(FORMULA_IDENTITY_FIELDS),
        "status_counts": dict(Counter(row["status"] for row in historical.values())),
        "identity_mismatches": 0,
    }


def _paired_runtime_report(
    historical: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    runtime_ratios = [
        float(historical[key]["runtime_seconds"])
        / float(reference[key]["runtime_seconds"])
        for key in sorted(reference)
    ]
    rss_ratios = [
        float(historical[key]["peak_memory_mb"])
        / float(reference[key]["peak_memory_mb"])
        for key in sorted(reference)
    ]
    return {
        "historical_over_current_runtime_median_ratio": statistics.median(
            runtime_ratios
        ),
        "historical_over_current_runtime_geometric_mean_ratio": math.exp(
            statistics.fmean(math.log(value) for value in runtime_ratios)
        ),
        "historical_over_current_rss_median_ratio": statistics.median(rss_ratios),
        "historical_solver_binary_sha256": sorted(
            {row.get("solver_binary_sha256", "") for row in historical.values()}
        ),
        "current_solver_binary_sha256": sorted(
            {row.get("solver_binary_sha256", "") for row in reference.values()}
        ),
        "runtime_sensitivity_required": True,
    }


def _archive_row(
    historical: dict[str, Any],
    reference: dict[str, Any],
    job: dict[str, Any],
    plan: dict[str, Any],
    *,
    historical_role: str,
) -> dict[str, Any]:
    row = dict(historical)
    original = dict(historical)
    for field in CANONICAL_CONFIGURATION_FIELDS:
        if field in reference:
            row[field] = reference[field]
    status = str(historical.get("status", ""))
    objective = _primary_value(historical) if status == "OPTIMAL" else ""
    proven = _proven_value(historical) if status == "OPTIMAL" else ""
    formula_signature = _formula_signature(historical)
    row.update(
        {
            "campaign_id": plan["campaign_id"],
            "experiment_block": job["experiment_block"],
            "planned_configuration_id": job["planned_configuration_id"],
            "repetition": job["repetition"],
            "run_order": job["run_order"],
            "run_key": job["run_key"],
            "attempt": 1,
            "run_order_seed": plan["run_order_seed"],
            "campaign_plan_sha256": plan["plan_sha256"],
            "instance": job["instance"],
            "instance_content_id": job["instance_content_id"],
            "instance_sha256": job["instance_sha256"],
            "base_lineage_id": job["base_lineage_id"],
            "instance_family": job["instance_family"],
            "instance_variant": job["instance_variant"],
            "instance_path": job["instance_path"],
            "objective_mode": job["configuration"]["objective_mode"],
            "compact_encoding": job["configuration"]["compact_encoding"],
            "collision_amo": job["configuration"]["collision_amo"],
            "participant_idle_cap_rule": "none",
            "objective_vector": objective,
            "proven_objective_vector": proven,
            "primary_objective_value": objective,
            "runtime_censored": str(status == "TIMEOUT"),
            "result_origin": "executed",
            "execution_shard_count": 1,
            "execution_shard_indices": "0",
            "execution_shard_index": 0,
            "execution_shard_policy": EXECUTION_SHARD_POLICY,
            "campaign_log": "",
            "archive_historical_role": historical_role,
            "archive_source_campaign_id": original.get(
                "campaign_id", "conference-2022-output"
            ),
            "archive_source_configuration_id": original.get("configuration_id", ""),
            "archive_source_configuration_label": original.get(
                "configuration_label", ""
            ),
            "archive_source_factor_m": original.get("factor_m", ""),
            "archive_source_factor_f": original.get("factor_f", ""),
            "archive_source_factor_p": original.get("factor_p", ""),
            "archive_source_factor_g": original.get("factor_g", ""),
            "archive_source_git_commit": original.get("git_commit", ""),
            "archive_source_git_dirty": original.get("git_dirty", ""),
            "archive_source_solver_binary_sha256": original.get(
                "solver_binary_sha256", ""
            ),
            "archive_source_row_sha256": sha256_text(canonical_json(original)),
            "archive_formula_identity_sha256": sha256_text(
                canonical_json(formula_signature)
            ),
            "archive_formula_identity_fields": ",".join(FORMULA_IDENTITY_FIELDS),
            "archive_formula_identity_verified": "True",
        }
    )
    return row


def prepare_archive(
    output: Path,
    *,
    config_path: Path = ARCHIVE_CONFIG,
    compact_csvs: Iterable[Path] = DEFAULT_COMPACT_CSVS,
    bg_local_csv: Path = DEFAULT_BG_LOCAL_CSV,
    bg_shard3_ref: str = DEFAULT_BG_SHARD3_REF,
    bg_shard3_path: str = DEFAULT_BG_SHARD3_PATH,
    ir_csvs: Iterable[Path] = DEFAULT_IR_CSVS,
) -> dict[str, Any]:
    output = output.resolve()
    existing_report: dict[str, Any] | None = None
    if output.exists() and any(output.iterdir()):
        errors, existing_report = validate_campaign(
            output, allow_dirty=True, allow_environment_drift=True
        )
        if errors:
            raise ValueError(
                f"refusing to overwrite invalid non-empty archive {output}: "
                + "; ".join(errors[:5])
            )

    compact_csvs = tuple(path.resolve() for path in compact_csvs)
    ir_csvs = tuple(path.resolve() for path in ir_csvs)
    bg_reference = _reference_rows(compact_csvs, "bg_d2")
    ir_reference = _reference_rows(compact_csvs, "ir")
    bg_historical, bg_provenance = _historical_bg_rows(
        bg_local_csv.resolve(), bg_shard3_ref, bg_shard3_path
    )
    ir_historical, ir_provenance = _historical_ir_rows(ir_csvs)
    bg_audit = _audit_equivalence(
        bg_historical, bg_reference, label="historical BG-d2"
    )
    ir_audit = _audit_equivalence(
        ir_historical, ir_reference, label="published conference IR"
    )
    runtime_sensitivity = _paired_runtime_report(ir_historical, ir_reference)

    config = read_config(config_path.resolve())
    plan = build_plan(config, resolve_datasets(config))
    unhashed_plan = dict(plan)
    unhashed_plan.pop("plan_sha256", None)
    unhashed_plan["archive_provenance"] = {
        "bg_d2": bg_provenance,
        "ir": ir_provenance,
        "equivalence_policy": (
            "exact formula signature, exact status, and exact primary optimum "
            "on every content"
        ),
        "historical_ir_runtime_sensitivity_required": True,
    }
    unhashed_plan["plan_sha256"] = sha256_text(canonical_json(unhashed_plan))
    plan = unhashed_plan

    if existing_report is not None:
        existing_plan = json.loads(
            (output / "plan.json").read_text(encoding="utf-8")
        )
        existing_equivalence = json.loads(
            (output / "equivalence_report.json").read_text(encoding="utf-8")
        )
        expected_provenance = {"bg_d2": bg_provenance, "ir": ir_provenance}
        if (
            existing_plan.get("plan_sha256") == plan["plan_sha256"]
            and existing_equivalence.get("source_provenance")
            == expected_provenance
        ):
            return existing_report
        raise ValueError(
            "the existing historical archive is valid but stale relative to "
            "the frozen configuration or source hashes; choose a new output path"
        )

    jobs = {(
        job["planned_configuration_id"], job["instance_content_id"]
    ): job for job in plan["jobs"]}
    records = []
    for source_id, role, historical_rows, reference_rows in (
        (
            "historical_bg_d2_reference",
            "bg_d2_independent_repetition_2",
            bg_historical,
            bg_reference,
        ),
        (
            "historical_ir_reference",
            "conference_ir_historical_repetition_2",
            ir_historical,
            ir_reference,
        ),
    ):
        for content_id, historical in historical_rows.items():
            job = jobs[(source_id, content_id)]
            row = _archive_row(
                historical,
                reference_rows[content_id],
                job,
                plan,
                historical_role=role,
            )
            records.append(
                {
                    "run_key": job["run_key"],
                    "attempt": 1,
                    "completed_utc": str(
                        historical.get("run_started_utc", "2026-07-23T00:00:00+00:00")
                    ),
                    "row": row,
                }
            )
    records.sort(key=lambda record: int(record["row"]["run_order"]))

    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "plan.json", plan)
    manifest = PROJECT_ROOT / "instances_manifest.csv"
    environment = {
        "created_utc": "2026-09-29T00:00:00+00:00",
        "campaign_id": plan["campaign_id"],
        "config_path": str(config_path.resolve().relative_to(PROJECT_ROOT)),
        "config_sha256": file_sha256(config_path.resolve()),
        "plan_sha256": plan["plan_sha256"],
        "git_commit": "MULTIPLE",
        "git_dirty": True,
        "requirements_sha256": "MULTIPLE",
        "manifest_sha256": {"instances_manifest.csv": file_sha256(manifest)},
        "uwrmaxsat_binary": "MULTIPLE",
        "uwrmaxsat_sha256": "MULTIPLE",
        "python_version": "MULTIPLE",
        "kernel_release": "MULTIPLE",
        "platform": "MULTIPLE",
        "hostname": "HISTORICAL_ARCHIVE",
        "boot_id": "",
        "cpu_model": "MULTIPLE",
        "physical_cpu_cores": 4,
        "logical_cpu_cores": 8,
        "system_memory_mb": 15988.0,
        "swap_memory_mb": 0,
        "required_machine": plan["required_machine"],
        "execution_shard_count": 1,
        "execution_shard_indices": [0],
        "execution_shard_policy": EXECUTION_SHARD_POLICY,
        "runner_command": "Prepare_Conference_Reuse.py",
        "environment_drift_accepted": True,
        "archive_only": True,
    }
    _write_json(output / "environment.json", environment)
    raw_path = output / "raw" / "results.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    rows = materialize_results(output)
    _write_json(
        output / "campaign_status.json",
        {
            "planned_jobs": len(plan["jobs"]),
            "latest_rows": len(rows),
            "terminal_rows": len(rows),
            "error_rows": 0,
            "archive_only": True,
            "plan_sha256": plan["plan_sha256"],
        },
    )
    equivalence_report = {
        "valid": True,
        "archive_campaign_id": plan["campaign_id"],
        "archive_plan_sha256": plan["plan_sha256"],
        "bg_d2": bg_audit,
        "ir": ir_audit,
        "historical_ir_environment_sensitivity": runtime_sensitivity,
        "source_provenance": {"bg_d2": bg_provenance, "ir": ir_provenance},
    }
    _write_json(output / "equivalence_report.json", equivalence_report)
    errors, validation_report = validate_campaign(
        output, allow_dirty=True, allow_environment_drift=True
    )
    _write_json(output / "validation_report.json", validation_report)
    if errors:
        raise ValueError(
            "prepared archive failed validation: " + "; ".join(errors[:10])
        )
    return validation_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the audited 252-row historical reuse archive for the "
            "conference-reference campaign."
        )
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default=str(ARCHIVE_CONFIG))
    parser.add_argument("--compact-reference-csv", action="append", default=[])
    parser.add_argument("--bg-local-csv", default=str(DEFAULT_BG_LOCAL_CSV))
    parser.add_argument("--bg-shard3-ref", default=DEFAULT_BG_SHARD3_REF)
    parser.add_argument("--bg-shard3-path", default=DEFAULT_BG_SHARD3_PATH)
    parser.add_argument("--ir-csv", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    compact_csvs = (
        tuple(Path(value) for value in args.compact_reference_csv)
        or DEFAULT_COMPACT_CSVS
    )
    ir_csvs = tuple(Path(value) for value in args.ir_csv) or DEFAULT_IR_CSVS
    try:
        report = prepare_archive(
            Path(args.output),
            config_path=Path(args.config),
            compact_csvs=compact_csvs,
            bg_local_csv=Path(args.bg_local_csv),
            bg_shard3_ref=args.bg_shard3_ref,
            bg_shard3_path=args.bg_shard3_path,
            ir_csvs=ir_csvs,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(
        "conference_historical_reuse "
        f"planned={report['planned_jobs']} "
        f"latest={report['latest_attempts']} valid={report['valid']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
