"""Prepare, audit, and resume the three-VM matched-encoding evaluation.

Preparation reads historical artifacts without changing them.  Only the run
subcommand invokes the existing single-worker experiment runner.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# A fresh GitHub checkout enters the complete published runtime before importing
# solver modules. The frozen archive retains the original standalone worker.
if __name__ == "__main__" and Path(__file__).with_name("Bootstrap_Matched_Evaluation.py").is_file():
    from Bootstrap_Matched_Evaluation import main as bootstrap_main
    raise SystemExit(bootstrap_main(["--worker", *sys.argv[1:]]))

from B2B_Instance import read_instance, validate_schedule_assignment
from Dataset_Manifest import file_sha256
from Journal_Experiment import (
    EXECUTION_SHARD_POLICY, PROJECT_ROOT, _read_jsonl, _write_json,
    build_environment, build_plan, canonical_json, execution_shard_assignments,
    latest_attempts, machine_profile_errors, materialize_results, read_config,
    resolve_datasets, sha256_text, utc_now,
)
from Journal_Metrics import evaluate_journal_schedule
from MaxSAT_Solver import executable_sha256, resolve_uwrmaxsat_binary
from Prepare_Conference_Reuse import (
    DEFAULT_BG_LOCAL_CSV, DEFAULT_BG_SHARD3_PATH, DEFAULT_BG_SHARD3_REF,
    FORMULA_IDENTITY_FIELDS, _audit_equivalence, _historical_bg_rows,
)

CONFIG = PROJECT_ROOT / "journal_configs/matched_encoding_evaluation_v2.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs/journal/matched-encoding-v2"
MAIN = PROJECT_ROOT / "outputs/journal/main-objectives-merged/normalized/detailed.csv"
COMPACT = PROJECT_ROOT / "outputs/journal/compact-objectives-merged/normalized/detailed.csv"
IR_RERUNS = PROJECT_ROOT / "outputs/journal/conference-ir-reruns/normalized/detailed.csv"
STATIC_FIELDS = tuple(f for f in FORMULA_IDENTITY_FIELDS if not f.startswith("optimizer_added_"))
MAIN_IDS = {
    "published_2022_bg_d2": "direct_bg_d2",
    "published_2022_ir": "direct_ir",
    "published_2022_ir_im_is": "direct_ir_im_is",
    "compact_acf_bg_d2": "compact_objective_bg_d2",
    "compact_cdf_ir": "compact_objective_ir",
    "compact_cdf_ir_im_is": "compact_objective_ir_im_is",
}
BASE_IDS = {
    "bg_reference": "compact_base_bg_d2",
    "ir_reference": "compact_base_ir",
    "irimis_reference": "compact_base_ir_im_is",
}
ENDPOINT_IDS = {
    "bg_acf": "compact_objective_bg_d2",
    "ir_cdf": "compact_objective_ir",
    "irimis_cdf": "compact_objective_ir_im_is",
}
EXPECTED_MISSING = {"All-126": 378, "Stress": 420, "Stress-high": 344}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(canonical_json(record) + "\n")


def relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def code_snapshot() -> dict[str, str]:
    files = sorted((PROJECT_ROOT / "src").glob("*.py")) + [
        PROJECT_ROOT / "src/requirements.txt", CONFIG,
        PROJECT_ROOT / "journal_configs/cross_generation_evaluation_blocks.json",
        PROJECT_ROOT / "scripts/run_matched_encoding_three_vms.sh",
    ]
    return {relative(path): file_sha256(path) for path in files}


def execution_identity(row: dict[str, Any], target: str) -> str:
    fields = [target, row.get("instance_sha256", "")]
    fields.extend(row.get(f, "") for f in (
        "run_started_utc", "hostname", "solver_binary_sha256"
    ))
    if not all(str(value).strip() for value in fields):
        raise ValueError("missing execution identity/provenance")
    return sha256_text(canonical_json(fields))


def recover_assignment(row: dict[str, Any], n_meetings: int) -> list[int]:
    """Return zero-based slots; ORG BG stores zero-based assignment vectors."""
    raw = row.get("assignment")
    if raw:
        values = list(raw) if isinstance(raw, (list, tuple)) else str(raw).split(",")
        zero_based = row.get("planned_configuration_id") in {"published_2022_bg_d2", "direct_bg_d2"}
        offset = int(row.get("assignment_index_base") if row.get("assignment_index_base") not in {None, ""} else (0 if zero_based else 1))
        assignment = [int(value) - offset for value in values]
    else:
        assignment = [-1] * n_meetings
        for chunk in str(row.get("schedule_by_slot", "")).split("|"):
            match = re.fullmatch(r"\s*(\d+)\s*:\s*(.*?)\s*", chunk)
            if not match:
                continue
            slot = int(match.group(1)) - 1
            for meeting_text in re.findall(r"M(\d+)", match.group(2)):
                meeting = int(meeting_text) - 1
                if not 0 <= meeting < n_meetings or assignment[meeting] != -1:
                    raise ValueError("duplicate/out-of-range meeting in schedule_by_slot")
                assignment[meeting] = slot
    if len(assignment) != n_meetings or any(value < 0 for value in assignment):
        raise ValueError("no complete assignment recoverable from source artifact")
    return assignment


def validate_source_row(row: dict[str, Any]) -> None:
    if row.get("status") not in {"OPTIMAL", "UNSAT", "TIMEOUT"}:
        raise ValueError(f"nonterminal source row: {row.get('status')}")
    expected_sat = {"OPTIMAL": {"SAT"}, "UNSAT": {"UNSAT"}, "TIMEOUT": {"TIMEOUT", "SAT", "UNKNOWN", ""}}
    if str(row.get("sat_result", "")) not in expected_sat[row["status"]]:
        raise ValueError("source status and SAT result disagree")
    for field in ("validation_errors", "error_type", "error_message"):
        if str(row.get(field, "")).strip():
            raise ValueError(f"source row has {field}")
    if str(row.get("campaign_executor_returncode", "")) not in {"", "0"}:
        raise ValueError("source executor failed")
    if float(row.get("timeout_seconds") or 7200) != 7200:
        raise ValueError("source uses a different timeout")
    if row.get("solver_backend") != "UWrMaxSAT":
        raise ValueError("source does not use UWrMaxSAT")
    if row.get("participant_idle_cap_rule", "none") not in {"", "none"}:
        raise ValueError("source uses participant idle caps")
    for field, expected in (("threads", 1), ("random_seed", 0)):
        if row.get(field) not in {None, ""} and int(float(row[field])) != expected:
            raise ValueError(f"source {field} differs from the frozen protocol")
    for field in ("runtime_seconds", "peak_memory_mb"):
        import math
        value = float(row[field])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid source {field}")
    if row["status"] == "TIMEOUT" and str(row.get("runtime_censored", "")).lower() != "true":
        raise ValueError("source timeout lacks censoring flag")


def prepare_evaluation(output: Path = DEFAULT_OUTPUT, *, plan_only: bool = False) -> dict[str, Any]:
    output = output.resolve()
    config = read_config(CONFIG)
    datasets = resolve_datasets(config)
    instances = {instance.sha256: instance for group in datasets.values() for instance in group}
    dataset_by_sha = {
        instance.sha256: display
        for dataset, display in (("official_all_126", "All-126"), ("stress", "Stress"), ("stress_high", "Stress-high"))
        for instance in datasets[dataset]
    }
    initial_plan = build_plan(config, datasets)
    jobs = {
        (job["planned_configuration_id"], job["instance_sha256"], job["repetition"]): job
        for job in initial_plan["jobs"]
    }
    selected: dict[str, dict[str, Any]] = {}
    cells: dict[str, dict[str, Any]] = {}
    execution_ids: set[str] = set()
    static_signatures: dict[tuple[str, str], tuple[str, ...]] = {}
    optimum_vectors: dict[tuple[str, str], str] = {}
    source_files: dict[str, str] = {}
    parsed_instances: dict[str, Any] = {}
    stats: Counter[str] = Counter()
    recovered_partials: dict[str, list[dict[str, Any]]] = {}

    def add(original: dict[str, Any], target: str, repetition: int,
            source_path: str, source_sha256: str) -> None:
        validate_source_row(original)
        sha = str(original.get("instance_sha256", ""))
        if sha not in instances:
            raise ValueError(f"unknown source instance: {sha}")
        job = jobs[(target, sha, repetition)]
        mode = job["configuration"]["objective_mode"]
        if original.get("objective_mode") != mode:
            raise ValueError("source and target objectives differ")
        if target.startswith("compact_"):
            for field in ("compact_encoding", "collision_amo", "domain_mode", "domain_filter_graph", "encoding_variant"):
                if original.get(field) != job["configuration"][field]:
                    raise ValueError(f"source/target configuration mismatch: {field}")
            if str(original.get("precedence_relation_edges", "")) != "0":
                for field in ("precedence_encoding", "precedence_graph"):
                    if original.get(field) != job["configuration"][field]:
                        raise ValueError(f"source/target configuration mismatch: {field}")
        elif original.get("domain_mode") not in {"full", "legacy_full"}:
            raise ValueError("Direct source does not use full domains")
        identity = execution_identity(original, target)
        if identity in execution_ids:
            raise ValueError("same source execution would occupy two target cells")
        if job["run_key"] in selected:
            raise ValueError("two sources target the same run key")
        signature = tuple(str(original.get(f, "")) for f in STATIC_FIELDS)
        signature_key = (target, sha)
        if signature_key in static_signatures and static_signatures[signature_key] != signature:
            raise ValueError(f"static formula mismatch: {target}/{sha}")
        static_signatures[signature_key] = signature

        row = dict(original)
        if row["status"] == "OPTIMAL":
            proven = str(row.get("proven_objective_vector") or row.get("objective_vector") or "")
            vector_key = (mode, sha)
            if not proven or (vector_key in optimum_vectors and optimum_vectors[vector_key] != proven):
                raise ValueError(f"proven optimum disagreement: {mode}/{sha}")
            optimum_vectors[vector_key] = proven
            if sha not in parsed_instances:
                instance = instances[sha]
                if file_sha256(instance.path) != sha:
                    raise ValueError("benchmark content hash mismatch")
                parsed_instances[sha] = read_instance(instance.path)
            instance = parsed_instances[sha]
            assignment = recover_assignment(original, instance.n_meetings)
            errors = validate_schedule_assignment(instance, assignment)
            metrics = evaluate_journal_schedule(instance, assignment, objective_mode=mode)
            if metrics.objective_vector != tuple(int(value) for value in proven.split(",")):
                errors.append("objective differs from independently evaluated schedule")
            if mode == "bg_d2" and not metrics.historical_fairness_cap_satisfied:
                errors.append("homogeneity constraint violated")
            if errors:
                raise ValueError(f"{target}/{sha}: {errors[:3]}")
            row["assignment"] = ",".join(str(value + 1) for value in assignment)
            row["assignment_index_base"] = 1
            row["schedule_validation"] = "independent_recomputed"
            stats["optimal_schedules_validated"] += 1
            if not original.get("assignment"):
                stats["assignments_recovered_from_schedule_by_slot"] += 1

        source_campaign = str(original.get("campaign_id") or "historical-execution")
        source_block = str(original.get("experiment_block") or "historical")
        source_id = str(original.get("planned_configuration_id") or original.get("configuration_id"))
        source_rep = int(original.get("repetition") or 1)
        source_run_key = str(original.get("run_key") or identity)
        source_row_hash = sha256_text(canonical_json(original))
        cells[job["run_key"]] = {
            "source_campaign_id": source_campaign, "source_block_id": source_block,
            "source_configuration_id": source_id, "source_repetition": source_rep,
            "source_run_key": source_run_key, "source_row_sha256": source_row_hash,
        }
        row.update({
            "campaign_id": config["campaign_name"], "run_key": job["run_key"],
            "experiment_block": job["experiment_block"],
            "planned_configuration_id": target, "repetition": repetition,
            "run_order": job["run_order"], "run_order_seed": initial_plan["run_order_seed"],
            "attempt": 1, "instance": job["instance"],
            "instance_content_id": job["instance_content_id"], "instance_sha256": sha,
            "instance_path": job["instance_path"], "instance_family": job["instance_family"],
            "instance_variant": job["instance_variant"], "base_lineage_id": job["base_lineage_id"],
            "result_origin": "reused", "reused_from_campaign_id": source_campaign,
            "reused_from_experiment_block": source_block, "reused_from_configuration_id": source_id,
            "reused_from_repetition": source_rep, "reused_from_run_key": source_run_key,
            "reused_from_plan_sha256": str(original.get("campaign_plan_sha256") or source_sha256),
            "reused_source_record_sha256": source_row_hash,
            "archive_source_path": source_path, "archive_source_file_sha256": source_sha256,
            "archive_source_row_sha256": source_row_hash,
            "archive_source_instance_content_id": original.get("instance_content_id", ""),
            "archive_execution_identity": identity, "archive_dataset": dataset_by_sha[sha],
            "campaign_log": "",
        })
        selected[job["run_key"]] = row
        execution_ids.add(identity)
        source_files[source_path] = source_sha256

    def add_csv(path: Path, mapping: dict[str, str], *, stress_only: bool = False,
                target_repetition: int | None = None) -> None:
        rows = read_csv(path)
        digest = file_sha256(path)
        for row in rows:
            cid = row.get("planned_configuration_id", "")
            if cid not in mapping:
                continue
            if stress_only and dataset_by_sha[row["instance_sha256"]] == "All-126":
                continue
            add(row, mapping[cid], target_repetition or int(row.get("repetition") or 1),
                relative(path), digest)

    # Source selection is fixed by the experimental design, never by runtime.
    add_csv(MAIN, MAIN_IDS)
    add_csv(COMPACT, BASE_IDS, target_repetition=1)
    historical_bg, historical_provenance = _historical_bg_rows(
        DEFAULT_BG_LOCAL_CSV, DEFAULT_BG_SHARD3_REF, DEFAULT_BG_SHARD3_PATH
    )
    reference_bg = {
        row["instance_content_id"]: row for row in read_csv(COMPACT)
        if row.get("planned_configuration_id") == "bg_reference"
    }
    _audit_equivalence(historical_bg, reference_bg, label="historical BG-d2 independent repetition")
    historical_hash = sha256_text(canonical_json(historical_provenance))
    for row in historical_bg.values():
        add(row, "compact_base_bg_d2", 2, "historical_bg_independent", historical_hash)
    ir_digest = file_sha256(IR_RERUNS)
    for row in read_csv(IR_RERUNS):
        add(row, "compact_base_ir", int(row["repetition"]) + 1, relative(IR_RERUNS), ir_digest)
    for path in sorted((PROJECT_ROOT / "outputs/journal/main-stress-sessions").glob("m*/normalized/detailed.csv")):
        add_csv(path, MAIN_IDS, stress_only=True, target_repetition=1)
    for path in sorted((PROJECT_ROOT / "outputs/journal/compact-ablation-stress-sessions").glob("a*/normalized/detailed.csv")):
        add_csv(path, {**BASE_IDS}, stress_only=True, target_repetition=1)
        add_csv(path, ENDPOINT_IDS, stress_only=True, target_repetition=2)
    for number in range(1, 7):
        path = PROJECT_ROOT / f"outputs/journal-rerun/main-stress-sessions/m{number:02d}/raw/results.jsonl"
        digest = file_sha256(path)
        records = latest_attempts(_read_jsonl(path))
        rows = [record["row"] for record in records.values()]
        if number in {5, 6}:
            recovered_partials[f"m{number:02d}"] = rows
        for row in rows:
            target = MAIN_IDS[row["planned_configuration_id"]]
            add(row, target, 2 if target.startswith("direct_") else 3, relative(path), digest)

    supplemental_rows = []
    for number in range(1, 7):
        path = PROJECT_ROOT / f"outputs/journal-rerun/main-objectives-single-rep-20261029/sessions/m{number:02d}/normalized/detailed.csv"
        rows = read_csv(path)
        if len(rows) != 126 or len({row["instance_sha256"] for row in rows}) != 126:
            raise ValueError("supplemental All-126 source is incomplete or duplicated")
        for row in rows:
            validate_source_row(row)
            target = MAIN_IDS[row["planned_configuration_id"]]
            sha = row["instance_sha256"]
            if tuple(str(row.get(field, "")) for field in STATIC_FIELDS) != static_signatures[(target, sha)]:
                raise ValueError("supplemental All-126 static formula mismatch")
            if row["status"] == "OPTIMAL":
                mode = row["objective_mode"]
                if str(row["proven_objective_vector"]) != optimum_vectors[(mode, sha)]:
                    raise ValueError("supplemental All-126 proven optimum mismatch")
                instance = parsed_instances[sha]
                assignment = recover_assignment(row, instance.n_meetings)
                if validate_schedule_assignment(instance, assignment):
                    raise ValueError("supplemental All-126 invalid schedule")
                metrics = evaluate_journal_schedule(instance, assignment, objective_mode=mode)
                if metrics.objective_vector != tuple(int(v) for v in row["proven_objective_vector"].split(",")):
                    raise ValueError("supplemental All-126 schedule/objective mismatch")
            supplemental_rows.append({
                "source_path": relative(path), "source_file_sha256": file_sha256(path),
                "source_row_sha256": sha256_text(canonical_json(row)),
                "target_configuration_id": target, "instance_sha256": sha,
                "status": row["status"], "runtime_seconds": row["runtime_seconds"],
                "execution_identity": execution_identity(row, target),
                "included_in_primary_three_repetitions": False,
            })
        source_files[relative(path)] = file_sha256(path)

    config.pop("blocks_from", None)
    config.pop("block_datasets", None)
    config.pop("resolved_blocks_from", None)
    config.pop("resolved_blocks_sha256", None)
    config["reuse_cells"] = cells
    config["frozen_sources"] = {
        "source_files": source_files, "historical_bg": historical_provenance,
        "code_files": code_snapshot(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip(),
    }
    plan = build_plan(config, datasets)
    shard_assignments = execution_shard_assignments(plan, 3)
    reuse_map: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for job in plan["jobs"]:
        dataset = dataset_by_sha[job["instance_sha256"]]
        item = {
            "target_run_key": job["run_key"], "target_configuration_id": job["planned_configuration_id"],
            "instance_content_id": job["instance_content_id"], "instance_sha256": job["instance_sha256"],
            "instance": job["instance"], "dataset": dataset, "repetition": job["repetition"],
            "shard_index": shard_assignments[job["run_key"]],
        }
        if job["run_key"] in selected:
            row = selected[job["run_key"]]
            row["campaign_plan_sha256"] = plan["plan_sha256"]
            item.update({**cells[job["run_key"]], "source_path": row["archive_source_path"],
                         "source_file_sha256": row["archive_source_file_sha256"]})
            reuse_map.append(item)
        else:
            remaining.append(item)
    missing_counts = dict(Counter(row["dataset"] for row in remaining))
    if len(selected) != 4960 or len(remaining) != 1142 or missing_counts != EXPECTED_MISSING:
        raise ValueError(f"frozen inventory mismatch: reused={len(selected)}, missing={missing_counts}")
    report = {
        "valid": True, "campaign_id": plan["campaign_id"], "plan_sha256": plan["plan_sha256"],
        "planned_jobs": 6102, "reused_jobs": 4960, "remaining_jobs": 1142,
        "remaining_by_dataset": missing_counts, "statistics": dict(stats),
        "shards": {
            str(index): {
                "planned_jobs": sum(value == index for value in shard_assignments.values()),
                "reused_jobs": sum(row["shard_index"] == index for row in reuse_map),
                "remaining_jobs": sum(row["shard_index"] == index for row in remaining),
            } for index in range(3)
        },
        "common_gcp_environment_assumed": True,
        "supplemental_all126_runs": len(supplemental_rows),
        "selection_policy": "fixed source roles and repetitions; no runtime-based selection",
        "historical_bg_provenance": historical_provenance,
    }
    if output.exists() and any(output.iterdir()):
        verify_bundle(output)
        old = json.loads((output / "plan.json").read_text())
        if old["plan_sha256"] != plan["plan_sha256"]:
            raise ValueError("refusing to overwrite a different frozen preparation; use a new output directory")
        return json.loads((output / "preparation_report.json").read_text())
    if plan_only:
        return report
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "campaign.json", config)
    _write_json(output / "plan.json", plan)
    write_csv(output / "reuse_map.csv", reuse_map)
    write_csv(output / "remaining_runs.csv", remaining)
    write_csv(output / "supplemental_runs.csv", supplemental_rows)
    records = [{"run_key": row["run_key"], "attempt": 1,
                "completed_utc": row.get("run_started_utc", ""), "row": row}
               for row in sorted(selected.values(), key=lambda row: int(row["run_order"]))]
    write_jsonl(output / "reused_records.jsonl", records)
    for name, rows in recovered_partials.items():
        from Main import write_detailed_csv
        write_detailed_csv(output / "recovered_sources" / name / "normalized/detailed.csv", rows)
    report["bundle_hashes"] = {
        name: file_sha256(output / name) for name in (
            "campaign.json", "plan.json", "reuse_map.csv", "remaining_runs.csv", "reused_records.jsonl", "supplemental_runs.csv"
        )
    }
    _write_json(output / "preparation_report.json", report)
    return report


def verify_bundle(output: Path) -> dict[str, Any]:
    report = json.loads((output / "preparation_report.json").read_text())
    for name, expected in report["bundle_hashes"].items():
        if file_sha256(output / name) != expected:
            raise ValueError(f"frozen preparation file changed: {name}")
    plan = json.loads((output / "plan.json").read_text())
    unhashed = dict(plan)
    expected = unhashed.pop("plan_sha256")
    if sha256_text(canonical_json(unhashed)) != expected:
        raise ValueError("frozen plan hash mismatch")
    return report


def seed_shard(output: Path, index: int) -> Path:
    if index not in range(3):
        raise ValueError("shard index must be 0, 1, or 2")
    verify_bundle(output)
    plan = json.loads((output / "plan.json").read_text())
    destination = output / f"shard-{index}-of-3"
    if (destination / "plan.json").exists():
        previous = json.loads((destination / "plan.json").read_text())
        if previous != plan:
            raise ValueError("existing shard has another frozen plan")
    elif destination.exists() and any(path.name != ".worker.lock" for path in destination.iterdir()):
        raise ValueError("refusing to seed a non-empty shard without a plan")
    assignments = execution_shard_assignments(plan, 3)
    raw_path = destination / "raw/results.jsonl"
    existing_records = _read_jsonl(raw_path)
    existing = latest_attempts(existing_records)
    if any(key not in assignments or assignments[key] != index for key in existing):
        raise ValueError("existing shard contains unassigned run keys")
    records = []
    for record in _read_jsonl(output / "reused_records.jsonl"):
        if assignments[record["run_key"]] != index:
            continue
        previous = existing.get(record["run_key"])
        if previous is not None:
            if (previous["row"].get("result_origin") != "reused" or
                previous["row"].get("reused_source_record_sha256") != record["row"].get("reused_source_record_sha256")):
                raise ValueError("existing row conflicts with a frozen reused result")
            continue
        record["row"].update({
            "execution_shard_count": 3, "execution_shard_indices": str(index),
            "execution_shard_index": index, "execution_shard_policy": EXECUTION_SHARD_POLICY,
        })
        records.append(record)
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "plan.json", plan)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if records:
        with raw_path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(canonical_json(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    materialize_results(destination)
    return destination


def run_shard(output: Path, index: int, *, binary: str, binary_sha256: str,
              allow_dirty: bool = False, cpu_core: int | None = None,
              retry_errors: bool = False) -> int:
    from Journal_Experiment import main as run_campaign
    import fcntl
    verify_bundle(output)
    config_path = output / "campaign.json"
    config = read_config(config_path)
    expected_files = config["frozen_sources"]["code_files"]
    if expected_files != code_snapshot():
        raise ValueError("source code differs from the frozen preparation")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip()
    if commit != config["frozen_sources"]["git_commit"]:
        raise ValueError("Git commit differs from the frozen preparation")
    datasets = resolve_datasets(config)
    for group in datasets.values():
        for instance in group:
            if file_sha256(instance.path) != instance.sha256:
                raise ValueError(f"benchmark content changed: {instance.instance_name}")
    if build_plan(config, datasets)["plan_sha256"] != json.loads((output / "plan.json").read_text())["plan_sha256"]:
        raise ValueError("configuration or dataset resolution differs from the frozen plan")
    errors = machine_profile_errors(config["required_machine"])
    if errors:
        raise ValueError("GCP profile mismatch: " + "; ".join(errors))
    resolved_binary = resolve_uwrmaxsat_binary(binary)
    if resolved_binary is None:
        raise ValueError("pinned UWrMaxSAT executable was not found")
    if not re.fullmatch(r"[0-9a-f]{64}", binary_sha256) or executable_sha256(resolved_binary) != binary_sha256:
        raise ValueError("production run requires the exact pinned UWrMaxSAT binary hash")
    if cpu_core is not None:
        os.sched_setaffinity(0, {cpu_core})
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    destination = output / f"shard-{index}-of-3"
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seed_shard(output, index)
        from Validate_Journal_Run import validate_campaign
        validation_errors, _ = validate_campaign(destination, allow_dirty=True,
            allow_incomplete=True, allow_environment_drift=True, allow_errors=retry_errors)
        validation_errors = [error for error in validation_errors if error != "missing environment.json"]
        if validation_errors:
            raise ValueError("shard failed pre-run validation: " + "; ".join(validation_errors[:5]))
        result = run_campaign([
            "--config", str(config_path), "--output-dir", str(destination),
            "--resume", "--shard-count", "3", "--shard-index", str(index),
            "--uwrmaxsat-bin", str(resolved_binary), "--uwrmaxsat-sha256", binary_sha256,
            "--allow-environment-drift", *(["--allow-dirty"] if allow_dirty else []),
            *(["--retry-errors"] if retry_errors else []),
        ])
    return result


def package_evaluation(output: Path, archive: Path) -> Path:
    """Export the exact source snapshot and preparation for a separate GCP checkout."""
    import tarfile
    verify_bundle(output)
    config = json.loads((output / "campaign.json").read_text())
    if config["frozen_sources"]["code_files"] != code_snapshot():
        raise ValueError("cannot package source that differs from the frozen preparation")
    if archive.exists():
        raise ValueError("refusing to overwrite an existing GCP package")
    try:
        archive.resolve().relative_to(output.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("GCP package must be outside the preparation directory")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(code_snapshot()):
            tar.add(PROJECT_ROOT / name, arcname=name)
        for path in sorted(output.rglob("*")):
            if path.is_file() and not any(part.startswith("shard-") or part == "merged" for part in path.relative_to(output).parts):
                tar.add(path, arcname="outputs/journal/matched-encoding-v2/" + path.relative_to(output).as_posix())
        instructions = PROJECT_ROOT / "MATCHED_EVALUATION_GCP.md"
        if instructions.exists():
            tar.add(instructions, arcname=instructions.name)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "plan", "run", "package"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--shard-index", type=int, choices=range(3))
    parser.add_argument("--uwrmaxsat-bin", default=os.environ.get("UWRMAXSAT_BIN", ""))
    parser.add_argument("--uwrmaxsat-sha256", default=os.environ.get("UWRMAXSAT_SHA256", ""))
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--cpu-core", type=int)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.command == "prepare":
        print(json.dumps(prepare_evaluation(output, plan_only=args.plan_only), indent=2, sort_keys=True))
    elif args.command == "package":
        if args.archive is None:
            parser.error("package requires --archive PATH.tar.gz")
        print(package_evaluation(output, args.archive))
    elif args.command == "plan" or args.plan_only:
        report = verify_bundle(output)
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if args.shard_index is None:
            parser.error("run requires --shard-index 0, 1, or 2")
        return run_shard(output, args.shard_index, binary=args.uwrmaxsat_bin,
                         binary_sha256=args.uwrmaxsat_sha256,
                         allow_dirty=args.allow_dirty, cpu_core=args.cpu_core,
                         retry_errors=args.retry_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
