"""Summarize schedule-quality differences across matched optimal solutions."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAITING_CONFIGURATIONS = {
    "total_idle": "waiting_is_compact_f",
    "squared_idle": "waiting_isq_compact_f",
    "maximum_then_total": "waiting_im_is_compact_f",
    "range_then_total": "waiting_ir_is_compact_f",
}
MEASURES = (
    "total_break_groups",
    "idle_range_pstar",
    "maximum_internal_idle_slots",
    "total_internal_idle_slots",
    "squared_internal_idle_slots",
)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def compare(
    left: dict[str, dict[str, str]],
    right: dict[str, dict[str, str]],
    keys: list[str],
    measure: str,
) -> dict[str, int]:
    lower = equal = higher = 0
    for key in keys:
        left_value = float(left[key][measure])
        right_value = float(right[key][measure])
        lower += left_value < right_value
        equal += left_value == right_value
        higher += left_value > right_value
    return {"lower": lower, "equal": equal, "higher": higher}


def summarize(
    waiting_rows: list[dict[str, str]],
    break_rows: list[dict[str, str]],
) -> dict[str, object]:
    groups: dict[str, dict[str, dict[str, str]]] = {}
    for name, configuration in WAITING_CONFIGURATIONS.items():
        groups[name] = {
            row["instance_content_id"]: row
            for row in waiting_rows
            if row["planned_configuration_id"] == configuration
            and row["status"] == "OPTIMAL"
        }
    groups["break_groups"] = {
        row["instance_content_id"]: row
        for row in break_rows
        if row["status"] == "OPTIMAL"
    }

    common = sorted(set.intersection(*(set(rows) for rows in groups.values())))
    medians = {
        name: {
            measure: median(float(rows[key][measure]) for key in common)
            for measure in MEASURES
        }
        for name, rows in groups.items()
    }
    comparisons = {
        left_name: {
            right_name: {
                measure: compare(
                    groups[left_name],
                    groups[right_name],
                    common,
                    measure,
                )
                for measure in MEASURES
            }
            for right_name in groups
            if right_name != left_name
        }
        for left_name in groups
    }
    return {
        "common_optimal_instance_count": len(common),
        "common_optimal_instance_content_ids": common,
        "medians": medians,
        "pairwise_comparisons": comparisons,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--waiting-results",
        type=Path,
        default=(
            PROJECT_ROOT
            / "outputs/journal/all126-first/shards-0-1-2-of-4/normalized/detailed.csv"
        ),
    )
    parser.add_argument(
        "--break-results",
        type=Path,
        default=(
            PROJECT_ROOT
            / "outputs/journal/bg-dinf-quality/normalized/detailed.csv"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs/objective_choice_summary.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = summarize(
        read_rows(args.waiting_results),
        read_rows(args.break_results),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"common_optimal={summary['common_optimal_instance_count']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
