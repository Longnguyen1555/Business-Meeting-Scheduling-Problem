from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Journal_Experiment import (
    TERMINAL_STATUSES,
    canonical_json,
    latest_attempts,
    machine_profile_errors,
    sha256_text,
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL line {line_number}: {exc}") from exc
    return records


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _objective_vector(row: dict[str, Any]) -> tuple[int, ...] | None:
    raw = row.get("objective_vector")
    if _is_blank(raw):
        return None
    if isinstance(raw, (list, tuple)):
        return tuple(int(value) for value in raw)
    return tuple(int(value.strip()) for value in str(raw).split(",") if value.strip())


def validate_campaign(
    output_dir: Path,
    *,
    allow_dirty: bool = False,
    allow_incomplete: bool = False,
    allow_errors: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    try:
        plan = _read_json(output_dir / "plan.json")
    except FileNotFoundError:
        return ["missing plan.json"], {}
    try:
        environment = _read_json(output_dir / "environment.json")
    except FileNotFoundError:
        environment = {}
        errors.append("missing environment.json")
    try:
        records = _read_jsonl(output_dir / "raw" / "results.jsonl")
        latest = latest_attempts(records)
    except (ValueError, KeyError) as exc:
        return [f"invalid results.jsonl: {exc}"], {}

    jobs = {job["run_key"]: job for job in plan.get("jobs", [])}
    unhashed_plan = dict(plan)
    recorded_plan_sha256 = unhashed_plan.pop("plan_sha256", "")
    recomputed_plan_sha256 = sha256_text(canonical_json(unhashed_plan))
    if recorded_plan_sha256 != recomputed_plan_sha256:
        errors.append("plan.json content does not match plan_sha256")
    if len(jobs) != int(plan.get("job_count", -1)):
        errors.append("plan job_count does not match unique run keys")
    extra = sorted(set(latest) - set(jobs))
    missing = sorted(set(jobs) - set(latest))
    if extra:
        errors.append(f"results contain {len(extra)} unplanned run keys")
    if missing and not allow_incomplete:
        errors.append(f"campaign is missing {len(missing)} planned run keys")
    normalized_path = output_dir / "normalized" / "detailed.csv"
    if normalized_path.is_file():
        with normalized_path.open(newline="", encoding="utf-8") as stream:
            normalized_rows = list(csv.DictReader(stream))
        normalized_keys = [row.get("run_key", "") for row in normalized_rows]
        if len(normalized_keys) != len(set(normalized_keys)):
            errors.append("normalized/detailed.csv has duplicate run keys")
        if set(normalized_keys) != set(latest):
            errors.append("normalized/detailed.csv is stale or incomplete")
        for row in normalized_rows:
            record = latest.get(row.get("run_key", ""))
            if record is not None and str(row.get("attempt", "")) != str(
                record.get("attempt", "")
            ):
                errors.append(
                    f"{row.get('run_key')}: normalized attempt is not latest"
                )
    elif latest:
        errors.append("missing normalized/detailed.csv")

    if environment:
        if environment.get("plan_sha256") != plan.get("plan_sha256"):
            errors.append("environment/plan SHA-256 mismatch")
        if environment.get("git_dirty") and not allow_dirty:
            errors.append("production provenance records git_dirty=true")
        if environment.get("required_machine", {}) != plan.get(
            "required_machine", {}
        ):
            errors.append("environment/plan required-machine profile mismatch")
        for mismatch in machine_profile_errors(
            plan.get("required_machine"), environment
        ):
            errors.append(f"production machine profile mismatch: {mismatch}")
    expected_uwr_hash = str(environment.get("uwrmaxsat_sha256", ""))
    required_machine = plan.get("required_machine", {})
    peak_memory_fraction = required_machine.get("max_peak_memory_fraction")
    system_memory_mb = environment.get("system_memory_mb")
    peak_memory_limit_mb = (
        float(peak_memory_fraction) * float(system_memory_mb)
        if peak_memory_fraction is not None and system_memory_mb is not None
        else None
    )

    status_counts: Counter[str] = Counter()
    content_ids: set[str] = set()
    lineage_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for run_key, record in latest.items():
        if run_key not in jobs:
            continue
        job = jobs[run_key]
        row = record.get("row", {})
        if record.get("run_key") != row.get("run_key"):
            errors.append(f"{run_key}: record/row run_key mismatch")
        rows.append(row)
        status = str(row.get("status", ""))
        status_counts[status] += 1
        if status == "ERROR" and not allow_errors:
            errors.append(f"{run_key}: terminal ERROR row")
        elif status not in TERMINAL_STATUSES and status != "ERROR":
            errors.append(f"{run_key}: unknown status {status!r}")
        for field in (
            "instance_content_id",
            "instance_sha256",
            "base_lineage_id",
            "experiment_block",
            "planned_configuration_id",
            "repetition",
            "run_order",
            "campaign_plan_sha256",
        ):
            expected = {
                "experiment_block": job["experiment_block"],
                "planned_configuration_id": job["planned_configuration_id"],
                "repetition": job["repetition"],
                "run_order": job["run_order"],
                "campaign_plan_sha256": plan["plan_sha256"],
            }.get(field, job.get(field))
            if str(row.get(field, "")) != str(expected):
                errors.append(
                    f"{run_key}: metadata mismatch for {field}: "
                    f"{row.get(field)!r}!={expected!r}"
                )
        expected_mode = job["configuration"].get("objective_mode", "")
        if str(row.get("objective_mode", "")) != str(expected_mode):
            errors.append(f"{run_key}: objective_mode mismatch")
        for field, requirement in (
            ("threads", required_machine.get("threads_per_run")),
            ("random_seed", required_machine.get("random_seed")),
        ):
            value = row.get(field)
            if requirement is not None and not _is_blank(value):
                if str(value) != str(requirement):
                    errors.append(
                        f"{run_key}: {field}={value!r}, expected {requirement!r}"
                    )
        if peak_memory_limit_mb is not None and status in TERMINAL_STATUSES:
            peak_memory_mb = row.get("peak_memory_mb")
            if _is_blank(peak_memory_mb):
                errors.append(f"{run_key}: missing peak_memory_mb")
            elif float(peak_memory_mb) > peak_memory_limit_mb:
                errors.append(
                    f"{run_key}: peak_memory_mb={peak_memory_mb} exceeds "
                    f"{float(peak_memory_fraction):.0%} of system RAM "
                    f"({peak_memory_limit_mb:.3f} MiB)"
                )
        if status == "OPTIMAL":
            if _objective_vector(row) is None:
                errors.append(f"{run_key}: OPTIMAL row has no objective vector")
            if not _is_blank(row.get("validation_errors")):
                errors.append(f"{run_key}: OPTIMAL row has validation errors")
            if _truthy(row.get("runtime_censored")):
                errors.append(f"{run_key}: OPTIMAL row is marked censored")
        if status == "TIMEOUT" and not _truthy(row.get("runtime_censored")):
            errors.append(f"{run_key}: TIMEOUT row is not marked censored")
        row_uwr_hash = str(row.get("solver_binary_sha256", ""))
        configuration = job["configuration"]
        requires_uwr = (
            configuration.get("executor") == "org_ir"
            or (
                configuration.get("executor") == "org_bg_d2"
                and configuration.get("backend", "uwrmaxsat") == "uwrmaxsat"
            )
            or (
                configuration.get("executor") == "main"
                and configuration.get("solver") == "maxsat"
                and configuration.get("maxsat_backend", "uwrmaxsat")
                == "uwrmaxsat"
            )
        )
        if requires_uwr and not row_uwr_hash:
            errors.append(f"{run_key}: missing required UWrMaxSAT binary hash")
        if row_uwr_hash and expected_uwr_hash and row_uwr_hash != expected_uwr_hash:
            errors.append(f"{run_key}: solver binary SHA-256 drift")
        content_ids.add(str(row.get("instance_content_id", "")))
        lineage_ids.add(str(row.get("base_lineage_id", "")))

    # Every exact representation/method solving the same content/objective and
    # repetition must agree. Timeouts are omitted, but OPTIMAL versus UNSAT is
    # always an inconsistency.
    agreement_groups: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in rows:
        agreement_groups[
            (
                str(row.get("instance_content_id", "")),
                str(row.get("objective_mode", "")),
                str(row.get("repetition", "")),
            )
        ].append(row)
    agreement_checked = 0
    for group_key, group_rows in agreement_groups.items():
        exact_rows = [
            row
            for row in group_rows
            if row.get("status") in {"OPTIMAL", "UNSAT"}
        ]
        statuses = {row.get("status") for row in exact_rows}
        if "OPTIMAL" in statuses and "UNSAT" in statuses:
            errors.append(f"objective/status disagreement for {group_key}")
            continue
        vectors = {
            _objective_vector(row)
            for row in exact_rows
            if row.get("status") == "OPTIMAL"
        }
        if len(vectors) > 1:
            errors.append(
                "objective-vector disagreement for "
                f"{group_key}: {sorted(vectors, key=str)}"
            )
        if len(exact_rows) >= 2:
            agreement_checked += 1

    # Diagnostic strata for the historical fairness cap. IR feasibility is a
    # base-problem witness; BG-d2 UNSAT after IR OPTIMAL is cap-induced UNSAT.
    by_content_rep: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_content_rep[
            (
                str(row.get("instance_content_id", "")),
                str(row.get("repetition", "")),
            )
        ].append(row)
    feasibility_classes: Counter[str] = Counter()
    for group_rows in by_content_rep.values():
        ir_statuses = {
            row.get("status")
            for row in group_rows
            if row.get("objective_mode") == "ir"
            and row.get("status") in {"OPTIMAL", "UNSAT"}
        }
        bg_statuses = {
            row.get("status")
            for row in group_rows
            if row.get("objective_mode") == "bg_d2"
            and row.get("status") in {"OPTIMAL", "UNSAT"}
        }
        if "UNSAT" in ir_statuses:
            feasibility_classes["base_unsat"] += 1
        elif "OPTIMAL" in ir_statuses and bg_statuses == {"UNSAT"}:
            feasibility_classes["base_feasible_bg_cap_unsat"] += 1
        elif "OPTIMAL" in bg_statuses:
            feasibility_classes["bg_d2_feasible"] += 1
        else:
            feasibility_classes["unclassified"] += 1

    report = {
        "validated_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "plan_sha256": plan.get("plan_sha256"),
        "planned_jobs": len(jobs),
        "latest_attempts": len(latest),
        "raw_attempts": len(records),
        "missing_jobs": len(missing),
        "extra_jobs": len(extra),
        "status_counts": dict(status_counts),
        "unique_contents": len(content_ids - {""}),
        "unique_lineages": len(lineage_ids - {""}),
        "agreement_groups_checked": agreement_checked,
        "peak_memory_limit_mb": peak_memory_limit_mb,
        "boot_ids": sorted(
            {
                str(row.get("campaign_boot_id", ""))
                for row in rows
                if not _is_blank(row.get("campaign_boot_id"))
            }
        ),
        "feasibility_classes": dict(feasibility_classes),
        "valid": not errors,
        "errors": errors,
    }
    return errors, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a journal campaign archive.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--allow-errors", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output).resolve()
    errors, report = validate_campaign(
        output,
        allow_dirty=args.allow_dirty,
        allow_incomplete=args.allow_incomplete,
        allow_errors=args.allow_errors,
    )
    report_path = output / "validation_report.json"
    with report_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(
        f"planned={report.get('planned_jobs')} latest={report.get('latest_attempts')} "
        f"valid={report.get('valid')} errors={len(errors)}"
    )
    for error in errors[:50]:
        print(f"ERROR: {error}")
    if len(errors) > 50:
        print(f"ERROR: ... {len(errors) - 50} additional errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
