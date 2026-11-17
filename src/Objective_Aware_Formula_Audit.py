"""Build matched compact formulas without invoking an optimizer.

The audit isolates the structural effect of attainable-value compression.  It
uses the same feasibility options for the base compact and objective-aware
compact encodings and records hard and soft formula measures for every input.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

from B2B_Instance import B2BSATModel, read_instance
from Main import collect_instances


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVES = ("is", "isq", "im_is", "ir_is")
ENCODINGS = ("optimized", "objective_aware")
FIELDNAMES = (
    "instance_name",
    "instance_content_id",
    "family",
    "objective_mode",
    "compact_encoding",
    "n_vars",
    "n_hard_clauses",
    "n_soft_clauses",
    "n_total_clauses",
    "n_hard_literals",
    "n_soft_literals",
    "n_total_literals",
    "soft_weight_sum",
    "idle_threshold_dense_level_count",
    "idle_threshold_encoded_level_count",
    "idle_threshold_removed_level_count",
    "objective_aggregate_dense_level_count",
    "objective_aggregate_encoded_level_count",
    "objective_constant_soft_weight",
)


def formula_row(instance_spec, objective_mode: str, compact_encoding: str):
    instance = read_instance(instance_spec.path)
    model = B2BSATModel(
        instance,
        precedence_encoding="sparse_suffix",
        precedence_graph="distance_closure",
        domain_filter_graph="distance_closure",
        encoding_variant="imp12+",
        domain_mode="reduced",
        objective_mode=objective_mode,
        compact_encoding=compact_encoding,
        collision_amo_encoding="adaptive_commander",
    )
    artifacts = model.build_base_cnf()
    soft_clauses = [
        clause
        for tier in artifacts.objective_tiers
        for clause in tier.maxsat_clauses
    ]
    soft_weights = [
        tier.scalar_weight * weight
        for tier in artifacts.objective_tiers
        for weight in tier.maxsat_weights
    ]
    return {
        "instance_name": instance_spec.instance_name,
        "instance_content_id": instance_spec.content_id,
        "family": instance_spec.family,
        "objective_mode": objective_mode,
        "compact_encoding": compact_encoding,
        "n_vars": artifacts.n_vars,
        "n_hard_clauses": artifacts.n_clauses,
        "n_soft_clauses": len(soft_clauses),
        "n_total_clauses": artifacts.n_clauses + len(soft_clauses),
        "n_hard_literals": artifacts.n_hard_literals,
        "n_soft_literals": sum(len(clause) for clause in soft_clauses),
        "n_total_literals": (
            artifacts.n_hard_literals
            + sum(len(clause) for clause in soft_clauses)
        ),
        "soft_weight_sum": sum(soft_weights),
        "idle_threshold_dense_level_count": (
            artifacts.idle_threshold_dense_level_count
        ),
        "idle_threshold_encoded_level_count": (
            artifacts.idle_threshold_encoded_level_count
        ),
        "idle_threshold_removed_level_count": (
            artifacts.idle_threshold_removed_level_count
        ),
        "objective_aggregate_dense_level_count": (
            artifacts.objective_aggregate_dense_level_count
        ),
        "objective_aggregate_encoded_level_count": (
            artifacts.objective_aggregate_encoded_level_count
        ),
        "objective_constant_soft_weight": (
            artifacts.objective_constant_soft_weight
        ),
    }


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    grouped: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        grouped[str(row["objective_mode"])][
            str(row["compact_encoding"])
        ].append(row)

    objectives: dict[str, object] = {}
    for objective, encodings in sorted(grouped.items()):
        base = {
            str(row["instance_content_id"]): row
            for row in encodings["optimized"]
        }
        compact = {
            str(row["instance_content_id"]): row
            for row in encodings["objective_aware"]
        }
        if set(base) != set(compact):
            raise ValueError(f"unmatched formula rows for {objective}")
        base_clauses = median(
            int(row["n_total_clauses"]) for row in base.values()
        )
        compact_clauses = median(
            int(row["n_total_clauses"]) for row in compact.values()
        )
        paired_differences = [
            int(base[key]["n_total_clauses"])
            - int(compact[key]["n_total_clauses"])
            for key in base
        ]
        paired_percentages = [
            100.0 * difference / int(base[key]["n_total_clauses"])
            for key, difference in zip(base, paired_differences)
        ]
        objectives[objective] = {
            "instances": len(base),
            "base_median_total_clauses": base_clauses,
            "objective_aware_median_total_clauses": compact_clauses,
            "reduction_between_medians_percent": (
                100.0 * (base_clauses - compact_clauses) / base_clauses
                if base_clauses
                else 0.0
            ),
            "paired_median_clause_reduction": median(paired_differences),
            "paired_median_clause_reduction_percent": median(
                paired_percentages
            ),
            "instances_with_fewer_clauses": sum(
                difference > 0 for difference in paired_differences
            ),
            "median_removed_idle_levels": median(
                int(row["idle_threshold_removed_level_count"])
                for row in compact.values()
            ),
        }
    return {"objectives": objectives, "row_count": len(rows)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "instances_manifest.csv",
    )
    parser.add_argument(
        "--family",
        choices=("all", "original", "forbidden", "fixed", "precedence"),
        default="all",
    )
    parser.add_argument("--max-instances", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "objective_aware_formula_audit.csv",
    )
    parser.add_argument(
        "--summarize-existing",
        action="store_true",
        help="read --output and rebuild only its JSON summary",
    )
    args = parser.parse_args()
    if args.max_instances is not None and args.max_instances <= 0:
        parser.error("--max-instances must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.summarize_existing:
        with args.output.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    else:
        instances = collect_instances(
            None,
            None,
            str(args.manifest.resolve()),
            args.family,
        )
        if args.max_instances is not None:
            instances = instances[: args.max_instances]

        rows = [
            formula_row(instance, objective, encoding)
            for instance in instances
            for objective in OBJECTIVES
            for encoding in ENCODINGS
        ]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summarize(rows), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"rows={len(rows)} csv={args.output} summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
