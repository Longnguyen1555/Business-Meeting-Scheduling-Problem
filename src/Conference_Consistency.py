"""Check the matched IR results against the frozen conference evidence."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from Prepare_Conference_Reuse import (
    DEFAULT_IR_CSVS, FORMULA_IDENTITY_FIELDS, _historical_ir_rows, _primary_value,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRESS_COMMIT = "95ea55b63072867e5da6388d2f8b5e1747bcb0dc"
STRESS_PATHS = (
    "output/rerun_old_B2B_Instance_full_matrix/"
    "data_precedence_stress/data_precedence_stress_detailed.csv",
    "output/rerun_old_B2B_Instance_full_matrix/"
    "data_precedence_stress_high/data_precedence_stress_high_detailed.csv",
)


def frozen_stress_bytes(path: str) -> bytes:
    """Read the frozen reference data, or the original Git object."""
    exported = PROJECT_ROOT / "frozen/conference" / path
    if exported.is_file():
        raw = exported.read_bytes()
    else:
        raw = subprocess.check_output(
            ["git", "show", f"{STRESS_COMMIT}:{path}"], cwd=PROJECT_ROOT)
    manifest = PROJECT_ROOT / "journal_configs/conference_consistency_sources.json"
    pins = json.loads(manifest.read_text())["sources"]
    expected = next(item for item in pins
                    if item["path"] == path and item.get("git_commit") == STRESS_COMMIT)
    if hashlib.sha256(raw).hexdigest() != expected["sha256"]:
        raise ValueError(f"conference source SHA-256 differs: {path}")
    return raw


def compare_ir_reference(
    reference: Sequence[dict], current: Sequence[dict], *, expected_instances: int,
    formula_fields: Sequence[str] = FORMULA_IDENTITY_FIELDS,
) -> dict:
    """Compare each repetition by content, never by aggregate solved counts."""
    source = {row["instance_sha256"]: row for row in reference}
    if len(source) != expected_instances or len(source) != len(reference):
        raise ValueError("conference reference contains missing or duplicate contents")
    reports = []
    for configuration in ("compact_base_ir", "compact_objective_ir"):
        for repetition in (1, 2, 3):
            group = [row for row in current
                     if row["planned_configuration_id"] == configuration
                     and int(row["repetition"]) == repetition]
            target = {row["instance_sha256"]: row for row in group}
            if len(target) != len(group) or set(target) != set(source):
                raise ValueError(f"conference content mismatch: {configuration}/{repetition}")
            for sha, historical in source.items():
                row = target[sha]
                if (row.get("objective_mode") != "ir"
                        or row.get("domain_mode") != "reduced"
                        or row.get("participant_idle_cap_rule", "none") not in ("", "none")):
                    raise ValueError(f"conference objective or feasible-set mismatch: {sha}")
                if row["status"] != historical["status"]:
                    raise ValueError(f"conference status mismatch: {configuration}/{sha}")
                if (row["status"] == "OPTIMAL"
                        and _primary_value(row) != _primary_value(historical)):
                    raise ValueError(f"conference optimum mismatch: {configuration}/{sha}")
                if configuration == "compact_base_ir":
                    for field in formula_fields:
                        if str(row.get(field, "")) != str(historical.get(field, "")):
                            raise ValueError(f"conference formula mismatch: {sha}/{field}")
            counts = Counter(row["status"] for row in group)
            reports.append({"configuration": configuration, "repetition": repetition,
                            "instances": len(group), "OPTIMAL": counts["OPTIMAL"],
                            "UNSAT": counts["UNSAT"], "TIMEOUT": counts["TIMEOUT"],
                            "Solved": counts["OPTIMAL"] + counts["UNSAT"],
                            "formula_fields_checked": len(formula_fields)
                            if configuration == "compact_base_ir" else 0})
    return {"valid": True, "instances": expected_instances, "checks": reports}


def conference_consistency_report(rows: Sequence[dict], *, manuscript: Path | None = None) -> dict:
    manuscript = manuscript or PROJECT_ROOT / "frozen/conference/conference_101719.tex"
    pinned = [{"path": "frozen/conference/conference_101719.tex", "location": "repository",
               "sha256": hashlib.sha256(manuscript.read_bytes()).hexdigest()}]
    official, _ = _historical_ir_rows(DEFAULT_IR_CSVS)
    for path in DEFAULT_IR_CSVS:
        pinned.append({"path": path.relative_to(PROJECT_ROOT).as_posix(),
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    stress = []
    for path in STRESS_PATHS:
        raw = frozen_stress_bytes(path)
        pinned.append({"path": path, "git_commit": STRESS_COMMIT,
                       "sha256": hashlib.sha256(raw).hexdigest()})
        stress.extend(row for row in csv.DictReader(io.StringIO(raw.decode("utf-8")))
                      if (row.get("factor_m"), row.get("factor_s"), row.get("factor_p"),
                          row.get("factor_g"))
                      == ("Reduced", "UWrMaxSAT", "SparseSuffix", "DistanceClosure-E*"))
    manifest_path = PROJECT_ROOT / "journal_configs/conference_consistency_sources.json"
    expected_sources = json.loads(manifest_path.read_text())["sources"]
    if pinned != expected_sources:
        raise ValueError("conference source SHA-256 differs from the pinned reference manifest")
    datasets = {}
    for name, reference, expected in (("All-126", list(official.values()), 126),
                                      ("Stress-100", stress, 100)):
        shas = {row["instance_sha256"] for row in reference}
        selected = [row for row in rows if row["planned_configuration_id"]
                    in ("compact_base_ir", "compact_objective_ir")
                    and row["instance_sha256"] in shas]
        datasets[name] = compare_ir_reference(reference, selected, expected_instances=expected)
    current_ir = {row["instance_sha256"] for row in rows
                  if row["planned_configuration_id"] in ("compact_base_ir", "compact_objective_ir")}
    reference_ir = {row["instance_sha256"] for row in (*official.values(), *stress)}
    if current_ir != reference_ir or len(reference_ir) != 226:
        raise ValueError("conference and JAIR IR benchmark contents differ")
    return {"valid": True, "checked_utc": datetime.now(timezone.utc).isoformat(),
            "sources": pinned, "mapping": "conference compact formulation = Compact-Base",
            "solved_definition": "OPTIMAL + UNSAT", "datasets": datasets,
            "runtime_comparison": "conference one-run values and JAIR three-run aggregates are distinct statistics"}
