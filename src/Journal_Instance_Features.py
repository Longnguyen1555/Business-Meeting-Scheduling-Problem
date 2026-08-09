from __future__ import annotations

import argparse
import csv
import math
import statistics
from bisect import bisect_right
from pathlib import Path
from typing import Any, Iterable

from B2B_Instance import B2BSATModel, original_eligible_slots, read_instance
from Main import InstanceSpec, collect_instances


PROJECT_ROOT = Path(__file__).resolve().parents[1]


FEATURE_FIELDS = (
    "dataset_id",
    "instance",
    "instance_content_id",
    "instance_sha256",
    "base_lineage_id",
    "instance_family",
    "instance_variant",
    "n_business",
    "n_meetings",
    "n_tables",
    "n_total_slots",
    "n_morning_slots",
    "participant_degree_mean",
    "participant_degree_std",
    "participant_degree_max",
    "participant_degree_cv",
    "participant_degree_gini",
    "unary_domain_width_mean",
    "unary_domain_width_std",
    "unary_domain_width_min",
    "unary_domain_width_max",
    "reduced_domain_width_mean",
    "reduced_domain_width_std",
    "reduced_domain_width_min",
    "reduced_domain_width_max",
    "domain_removal_ratio",
    "domain_filter_iterations",
    "domain_filter_seconds",
    "preprocessing_feasible",
    "capacity_pressure",
    "fixed_ratio",
    "forbidden_ratio",
    "session_restricted_ratio",
    "precedence_direct_edges",
    "precedence_closure_edges",
    "precedence_direct_density",
    "precedence_closure_density",
    "precedence_longest_path",
    "precedence_mean_reachable_distance",
    "precedence_unique_suffix_cuts",
)


def _mean(values: list[int]) -> float:
    return statistics.fmean(values) if values else 0.0


def _std(values: list[int]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def gini(values: Iterable[int]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered or sum(ordered) == 0:
        return 0.0
    n = len(ordered)
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return (2 * weighted) / (n * sum(ordered)) - (n + 1) / n


def extract_instance_features(
    spec: InstanceSpec,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    instance = read_instance(spec.path)
    model = B2BSATModel(
        instance,
        precedence_encoding="sparse_suffix",
        precedence_graph="distance_closure",
        domain_filter_graph="distance_closure",
        encoding_variant="basic",
        domain_mode="reduced",
        objective_mode="ir",
    )
    degrees = list(instance.n_meetings_business)
    unary_widths = [
        len(original_eligible_slots(instance, meeting))
        for meeting in range(instance.n_meetings)
    ]
    reduced_widths = [
        len(model.eligible_slots(meeting))
        for meeting in range(instance.n_meetings)
    ]
    unary_total = sum(unary_widths)
    possible_relations = instance.n_meetings * (instance.n_meetings - 1) / 2
    reachable_distances = [
        distance
        for distances in model.graph.longest_distance
        for distance in distances.values()
    ]
    cuts_by_predecessor: dict[int, set[int]] = {}
    for post, distances in enumerate(model.graph.longest_distance):
        post_slots = model.eligible_slots(post)
        for predecessor, distance in distances.items():
            predecessor_slots = model.eligible_slots(predecessor)
            for post_slot in post_slots:
                split = bisect_right(
                    predecessor_slots,
                    post_slot - distance,
                )
                if 0 < split < len(predecessor_slots):
                    cuts_by_predecessor.setdefault(predecessor, set()).add(split)
    degree_mean = _mean(degrees)
    return {
        "dataset_id": dataset_id,
        "instance": spec.instance_name,
        "instance_content_id": spec.content_id,
        "instance_sha256": spec.sha256,
        "base_lineage_id": spec.base_lineage_id,
        "instance_family": spec.family,
        "instance_variant": spec.variant,
        "n_business": instance.n_business,
        "n_meetings": instance.n_meetings,
        "n_tables": instance.n_tables,
        "n_total_slots": instance.n_total_slots,
        "n_morning_slots": instance.n_morning_slots,
        "participant_degree_mean": round(degree_mean, 6),
        "participant_degree_std": round(_std(degrees), 6),
        "participant_degree_max": max(degrees, default=0),
        "participant_degree_cv": round(
            _std(degrees) / degree_mean if degree_mean else 0.0,
            6,
        ),
        "participant_degree_gini": round(gini(degrees), 6),
        "unary_domain_width_mean": round(_mean(unary_widths), 6),
        "unary_domain_width_std": round(_std(unary_widths), 6),
        "unary_domain_width_min": min(unary_widths, default=0),
        "unary_domain_width_max": max(unary_widths, default=0),
        "reduced_domain_width_mean": round(_mean(reduced_widths), 6),
        "reduced_domain_width_std": round(_std(reduced_widths), 6),
        "reduced_domain_width_min": min(reduced_widths, default=0),
        "reduced_domain_width_max": max(reduced_widths, default=0),
        "domain_removal_ratio": round(
            1 - sum(reduced_widths) / unary_total if unary_total else 0.0,
            6,
        ),
        "domain_filter_iterations": model.domain_filter_iterations,
        "domain_filter_seconds": round(model.domain_filter_seconds, 6),
        "preprocessing_feasible": (
            not model.graph.cycle_nodes and all(reduced_widths)
        ),
        "capacity_pressure": round(
            instance.n_meetings
            / (instance.n_tables * instance.n_total_slots)
            if instance.n_tables and instance.n_total_slots
            else math.inf,
            6,
        ),
        "fixed_ratio": round(
            sum(slot is not None for slot in instance.fixed)
            / instance.n_meetings
            if instance.n_meetings
            else 0.0,
            6,
        ),
        "forbidden_ratio": round(
            sum(len(slots) for slots in instance.forbidden)
            / (instance.n_business * instance.n_total_slots)
            if instance.n_business and instance.n_total_slots
            else 0.0,
            6,
        ),
        "session_restricted_ratio": round(
            sum(session != 3 for _, _, session in instance.requested)
            / instance.n_meetings
            if instance.n_meetings
            else 0.0,
            6,
        ),
        "precedence_direct_edges": model.graph.direct_edge_count,
        "precedence_closure_edges": model.graph.transitive_edge_count,
        "precedence_direct_density": round(
            model.graph.direct_edge_count / possible_relations
            if possible_relations
            else 0.0,
            8,
        ),
        "precedence_closure_density": round(
            model.graph.transitive_edge_count / possible_relations
            if possible_relations
            else 0.0,
            8,
        ),
        "precedence_longest_path": model.graph.max_chain_distance,
        "precedence_mean_reachable_distance": round(
            statistics.fmean(reachable_distances)
            if reachable_distances
            else 0.0,
            6,
        ),
        "precedence_unique_suffix_cuts": sum(
            len(cuts) for cuts in cuts_by_predecessor.values()
        ),
    }


def write_features(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FEATURE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract pre-solve structural features for journal analysis."
    )
    parser.add_argument(
        "--dataset",
        action="append",
        required=True,
        metavar="ID:MANIFEST",
        help="repeatable dataset id and manifest path",
    )
    parser.add_argument(
        "--family",
        choices=["all", "original", "forbidden", "fixed", "precedence"],
        default="all",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output)
    if output.exists() and not args.overwrite:
        print(f"ERROR: output exists: {output}; pass --overwrite explicitly")
        return 2
    rows: list[dict[str, Any]] = []
    seen_content: set[str] = set()
    for dataset_value in args.dataset:
        try:
            dataset_id, manifest_value = dataset_value.split(":", 1)
        except ValueError:
            print(f"ERROR: invalid --dataset value: {dataset_value!r}")
            return 2
        manifest = Path(manifest_value)
        if not manifest.is_absolute():
            manifest = PROJECT_ROOT / manifest
        instances = collect_instances(
            None,
            None,
            str(manifest),
            args.family,
        )
        for index, spec in enumerate(instances, start=1):
            if spec.content_id in seen_content:
                print(
                    "ERROR: duplicate content across selected datasets: "
                    f"{spec.content_id}"
                )
                return 2
            seen_content.add(spec.content_id)
            rows.append(extract_instance_features(spec, dataset_id=dataset_id))
            print(
                f"[{dataset_id} {index}/{len(instances)}] {spec.instance_name}",
                flush=True,
            )
    write_features(output, rows)
    print(f"wrote {len(rows)} feature rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
