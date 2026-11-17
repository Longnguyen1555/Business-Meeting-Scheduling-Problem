from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


HISTORICAL_ROLE = "conference_ir_historical_repetition_2"
CONFERENCE_IR_CONFIGURATION = "conference_ir"


def read_rows(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                run_key = row.get("run_key", "")
                if run_key and run_key in seen:
                    continue
                if run_key:
                    seen.add(run_key)
                rows.append(row)
    return rows


def _par2(row: dict[str, str]) -> float:
    timeout = float(row.get("timeout_seconds") or 7200.0)
    if row.get("status") == "TIMEOUT":
        return 2.0 * timeout
    return float(row["runtime_seconds"])


def _configuration_scores(
    rows: Iterable[dict[str, str]], *, exclude_historical: bool
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("objective_mode") != "ir":
            continue
        if exclude_historical and row.get("archive_historical_role") == HISTORICAL_ROLE:
            continue
        groups[row["planned_configuration_id"]].append(row)
    scores = []
    for configuration_id, group in groups.items():
        par2 = [_par2(row) for row in group]
        memory = [
            float(row["peak_memory_mb"])
            for row in group
            if row.get("peak_memory_mb")
        ]
        scores.append(
            {
                "configuration_id": configuration_id,
                "rows": len(group),
                "contents": len({row["instance_content_id"] for row in group}),
                "mean_par2_seconds": statistics.fmean(par2),
                "median_runtime_seconds": statistics.median(
                    float(row["runtime_seconds"]) for row in group
                ),
                "median_peak_memory_mb": statistics.median(memory) if memory else None,
            }
        )
    return sorted(scores, key=lambda item: item["configuration_id"])


def _ranking(scores: list[dict[str, Any]], field: str) -> list[str]:
    return [
        item["configuration_id"]
        for item in sorted(
            scores,
            key=lambda item: (
                float("inf") if item[field] is None else item[field],
                item["configuration_id"],
            ),
        )
    ]


def audit_sensitivity(
    conference_rows: list[dict[str, str]],
    comparison_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    conference_ir = [
        row
        for row in conference_rows
        if row.get("planned_configuration_id") == CONFERENCE_IR_CONFIGURATION
        and row.get("objective_mode") == "ir"
    ]
    if len(conference_ir) != 378:
        raise ValueError(
            f"conference IR must contain 378 rows (126 x 3), got {len(conference_ir)}"
        )
    per_content = Counter(row["instance_content_id"] for row in conference_ir)
    if len(per_content) != 126 or set(per_content.values()) != {3}:
        raise ValueError(
            "conference IR does not contain exactly three rows per content"
        )
    historical = [
        row
        for row in conference_ir
        if row.get("archive_historical_role") == HISTORICAL_ROLE
    ]
    if len(historical) != 126:
        raise ValueError(
            f"expected 126 historical IR rows, got {len(historical)}"
        )
    if len({row["instance_content_id"] for row in historical}) != 126:
        raise ValueError("historical IR rows do not cover 126 unique contents")

    current_only = [row for row in conference_ir if row not in historical]
    included_par2 = statistics.fmean(_par2(row) for row in conference_ir)
    excluded_par2 = statistics.fmean(_par2(row) for row in current_only)
    included_runtime = statistics.median(
        float(row["runtime_seconds"]) for row in conference_ir
    )
    excluded_runtime = statistics.median(
        float(row["runtime_seconds"]) for row in current_only
    )
    included_rss = statistics.median(
        float(row["peak_memory_mb"])
        for row in conference_ir
        if row.get("peak_memory_mb")
    )
    excluded_rss = statistics.median(
        float(row["peak_memory_mb"])
        for row in current_only
        if row.get("peak_memory_mb")
    )

    all_rows = list(conference_rows)
    if comparison_rows:
        all_rows.extend(comparison_rows)
    included_scores = _configuration_scores(all_rows, exclude_historical=False)
    excluded_scores = _configuration_scores(all_rows, exclude_historical=True)
    ranking_report = {}
    for field in (
        "mean_par2_seconds",
        "median_runtime_seconds",
        "median_peak_memory_mb",
    ):
        included = _ranking(included_scores, field)
        excluded = _ranking(excluded_scores, field)
        ranking_report[field] = {
            "with_historical": included,
            "without_historical": excluded,
            "unchanged": included == excluded,
        }
    return {
        "valid": True,
        "historical_role": HISTORICAL_ROLE,
        "conference_ir_rows": len(conference_ir),
        "historical_rows": len(historical),
        "current_environment_rows": len(current_only),
        "conference_ir_metrics": {
            "mean_par2_seconds": {
                "with_historical": included_par2,
                "without_historical": excluded_par2,
                "ratio": included_par2 / excluded_par2,
            },
            "median_runtime_seconds": {
                "with_historical": included_runtime,
                "without_historical": excluded_runtime,
                "ratio": included_runtime / excluded_runtime,
            },
            "median_peak_memory_mb": {
                "with_historical": included_rss,
                "without_historical": excluded_rss,
                "ratio": included_rss / excluded_rss,
            },
        },
        "configuration_scores_with_historical": included_scores,
        "configuration_scores_without_historical": excluded_scores,
        "rankings": ranking_report,
        "all_rankings_unchanged": all(
            item["unchanged"] for item in ranking_report.values()
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare IR runtime/PAR-2/RSS summaries with and without the "
            "published historical conference repetition."
        )
    )
    parser.add_argument("--conference-csv", action="append", required=True)
    parser.add_argument("--comparison-csv", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--require-stable-ranking",
        action="store_true",
        help="return a non-zero status if any reported ranking changes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        conference_rows = read_rows(Path(value) for value in args.conference_csv)
        comparison_rows = read_rows(Path(value) for value in args.comparison_csv)
        report = audit_sensitivity(conference_rows, comparison_rows)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(
        "conference_runtime_sensitivity "
        f"historical_rows={report['historical_rows']} "
        f"all_rankings_unchanged={report['all_rankings_unchanged']}"
    )
    if args.require_stable_ranking and not report["all_rankings_unchanged"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
