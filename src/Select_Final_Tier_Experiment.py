from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from Journal_Experiment import read_config
from Validate_Journal_Run import validate_campaign


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def rank_ir_im_is_configurations(
    rows: list[dict[str, Any]],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Rank exact IR-IM-IS configurations by coverage, PAR-2, then memory."""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("objective_mode") == "ir_im_is":
            grouped[str(row["planned_configuration_id"])].append(row)

    scores: list[dict[str, Any]] = []
    for configuration_id, group in grouped.items():
        exact = sum(
            str(row.get("status")) in {"OPTIMAL", "UNSAT"} for row in group
        )
        par2_values = []
        memory_values = []
        for row in group:
            status = str(row.get("status"))
            try:
                runtime = float(row.get("runtime_seconds", timeout_seconds))
            except (TypeError, ValueError):
                runtime = timeout_seconds
            par2_values.append(
                min(runtime, timeout_seconds)
                if status in {"OPTIMAL", "UNSAT"}
                else 2.0 * timeout_seconds
            )
            try:
                memory_values.append(float(row["peak_memory_mb"]))
            except (KeyError, TypeError, ValueError):
                pass
        score = {
            "configuration_id": configuration_id,
            "runs": len(group),
            "exact_runs": exact,
            "par2_seconds": sum(par2_values) / len(par2_values),
            "median_peak_memory_mb": (
                median(memory_values) if memory_values else None
            ),
        }
        scores.append(score)

    return sorted(
        scores,
        key=lambda item: (
            -item["exact_runs"],
            item["par2_seconds"],
            (
                float(item["median_peak_memory_mb"])
                if item["median_peak_memory_mb"] is not None
                else float("inf")
            ),
            item["configuration_id"],
        ),
    )


def build_final_tier_config(
    main_config: dict[str, Any],
    main_plan: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    timeout = float(main_plan["timeout_seconds"])
    ranking = rank_ir_im_is_configurations(rows, timeout_seconds=timeout)
    if not ranking:
        raise ValueError("main campaign contains no IR-IM-IS result rows")
    selected_id = ranking[0]["configuration_id"]
    selected: dict[str, Any] | None = None
    for job in main_plan["jobs"]:
        configuration = job["configuration"]
        if configuration["id"] == selected_id:
            selected = dict(configuration)
            break
    if selected is None:
        raise ValueError(f"selected configuration {selected_id!r} is absent from plan")

    ir_im_isq = dict(
        selected,
        id="selected_model_ir_im_isq",
        objective_mode="ir_im_isq",
    )
    repetitions = 3
    unique_contents = len(
        {str(job["instance_content_id"]) for job in main_plan["jobs"]}
    )
    config = {
        "schema_version": 1,
        "campaign_name": "journal-final-tier-isq-only-v1",
        "description": (
            "Uncapped IR-IM-ISQ on the model selected from the primary "
            "IR-IM-IS campaign; existing main-campaign IR-IM-IS rows are "
            "reused as the comparison baseline."
        ),
        "timeout_seconds": timeout,
        "controller_grace_seconds": float(
            main_plan.get("controller_grace_seconds", 120.0)
        ),
        "run_order_seed": int(main_config.get("run_order_seed", 0)) + 1,
        "require_clean_worktree": bool(
            main_config.get("require_clean_worktree", True)
        ),
        "required_machine": dict(main_config.get("required_machine", {})),
        "expected_job_count": unique_contents * repetitions,
        "datasets": list(main_config["datasets"]),
        "blocks": [
            {
                "id": "selected_model_final_tier_comparison",
                "datasets": [item["id"] for item in main_config["datasets"]],
                "repetitions": repetitions,
                "configurations": [ir_im_isq],
            }
        ],
        "selection_provenance": {
            "source_campaign_id": main_plan["campaign_id"],
            "source_plan_sha256": main_plan["plan_sha256"],
            "selected_configuration_id": selected_id,
            "baseline_objective_mode": "ir_im_is",
            "new_objective_mode": "ir_im_isq",
            "baseline_results_reused": True,
            "ranking_rule": "exact_coverage_desc_then_par2_then_median_rss_then_id",
        },
    }
    report = {
        **config["selection_provenance"],
        "ranking": ranking,
    }
    return config, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select the best main IR-IM-IS model and emit final-tier config."
    )
    parser.add_argument("--main-output", required=True, type=Path)
    parser.add_argument("--main-config", required=True, type=Path)
    parser.add_argument("--output-config", required=True, type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors, _ = validate_campaign(
        args.main_output,
        allow_dirty=args.allow_dirty,
    )
    if errors:
        raise SystemExit(
            "ERROR: main campaign must validate before selection: "
            + "; ".join(errors)
        )
    main_config = read_config(args.main_config)
    main_plan = _read_json(args.main_output / "plan.json")
    rows = _read_rows(args.main_output / "normalized" / "detailed.csv")
    config, report = build_final_tier_config(main_config, main_plan, rows)
    args.output_config.parent.mkdir(parents=True, exist_ok=True)
    args.output_config.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report_path = args.output_config.with_suffix(".selection.json")
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"selected_configuration={report['selected_configuration_id']}")
    print(f"wrote_config={args.output_config}")
    print(f"wrote_selection_report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
