from __future__ import annotations

import argparse
import json
from pathlib import Path

from B2B_Instance import B2BSATModel, read_instance
from B2B_Instance_CompactV2 import B2BSATModelV2


def summary(label: str, model) -> dict[str, object]:
    a = model.build_base_cnf()
    row: dict[str, object] = {
        "label": label,
        "objective_mode": a.objective_mode,
        "objective": a.objective_name,
        "domain_mode": a.domain_mode,
        "domain_filter_graph": a.domain_filter_graph,
        "precedence_encoding": a.precedence_encoding,
        "precedence_graph": a.precedence_graph,
        "vars": a.n_vars,
        "primary_vars": a.n_primary_variables,
        "aux_vars": a.n_auxiliary_variables,
        "clauses": a.n_clauses,
        "hard_literals": a.n_hard_literals,
        "binary": a.n_binary_hard_clauses,
        "ternary": a.n_ternary_hard_clauses,
        "long": a.n_long_hard_clauses,
        "active_schedule_candidates": a.active_schedule_candidates,
        "precedence_relations": a.precedence_relation_edges,
        "precedence_links": a.precedence_sparse_link_clauses,
        "precedence_unique_cuts": a.precedence_unique_suffix_cuts,
        "objective_tiers": [
            {
                "name": tier.name,
                "literals": len(tier.literals),
                "upper_bound": tier.upper_bound,
                "scalar_weight": tier.scalar_weight,
            }
            for tier in a.objective_tiers
        ],
    }
    if hasattr(model, "compact_v2_metadata"):
        row.update(model.compact_v2_metadata())
    return row


def pct(new: int, old: int) -> float | None:
    if old == 0:
        return None
    return round(100.0 * (new - old) / old, 3)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build current and Compact-v2 B2B CNFs and compare formula metrics."
    )
    parser.add_argument("instance", type=Path)
    parser.add_argument(
        "--objective-mode",
        default="ir",
        choices=[
            "ir",
            "bg_d2",
            "ir_is",
            "bg_ir_is",
            "max_idle_is",
            "sla_idle",
        ],
    )
    parser.add_argument(
        "--precedence-encoding",
        default="sparse_suffix",
        choices=["pairwise", "sparse_suffix", "hybrid_suffix"],
    )
    parser.add_argument(
        "--precedence-graph",
        default="distance_closure",
        choices=["direct", "distance_closure"],
    )
    parser.add_argument(
        "--domain-filter-graph",
        default="distance_closure",
        choices=["direct", "distance_closure"],
    )
    parser.add_argument(
        "--capacity-mode",
        default="slot_cluster",
        choices=[
            "meeting",
            "global_cluster",
            "slot_cluster",
            "busy",
            "slot_cluster_busy",
        ],
    )
    parser.add_argument(
        "--capacity-cardinality",
        default="seqcounter",
        choices=["seqcounter", "cardnet", "totalizer", "adaptive"],
    )
    parser.add_argument(
        "--bg-counter-mode",
        default="shared_dp",
        choices=["repeated_seqcounter", "shared_dp"],
    )
    parser.add_argument("--idle-sla-threshold", type=int, default=2)
    parser.add_argument("--hybrid-suffix-density", type=float, default=0.70)
    parser.add_argument("--write-v2-cnf", type=Path)
    args = parser.parse_args()

    inst = read_instance(args.instance)

    baseline_objective = (
        args.objective_mode
        if args.objective_mode in {"ir", "bg_d2", "ir_is", "bg_ir_is"}
        else "ir"
    )
    baseline_precedence = (
        args.precedence_encoding
        if args.precedence_encoding in {"pairwise", "sparse_suffix"}
        else "sparse_suffix"
    )

    current = B2BSATModel(
        inst=inst,
        precedence_encoding=baseline_precedence,
        precedence_graph=args.precedence_graph,
        domain_filter_graph=args.domain_filter_graph,
        encoding_variant="imp12+",
        domain_mode="reduced",
        objective_mode=baseline_objective,
    )
    v2 = B2BSATModelV2(
        inst=inst,
        precedence_encoding=args.precedence_encoding,
        precedence_graph=args.precedence_graph,
        domain_filter_graph=args.domain_filter_graph,
        encoding_variant="imp12+",
        domain_mode="reduced",
        objective_mode=args.objective_mode,
        capacity_mode=args.capacity_mode,
        capacity_cardinality=args.capacity_cardinality,
        bg_counter_mode=args.bg_counter_mode,
        idle_sla_threshold=args.idle_sla_threshold,
        hybrid_suffix_density=args.hybrid_suffix_density,
    )

    old = summary("current", current)
    new = summary("compact_v2", v2)

    comparison = {
        "vars_pct": pct(int(new["vars"]), int(old["vars"])),
        "aux_vars_pct": pct(int(new["aux_vars"]), int(old["aux_vars"])),
        "clauses_pct": pct(int(new["clauses"]), int(old["clauses"])),
        "hard_literals_pct": pct(
            int(new["hard_literals"]), int(old["hard_literals"])
        ),
    }

    print(json.dumps({"current": old, "compact_v2": new, "delta": comparison}, indent=2))

    if args.write_v2_cnf is not None:
        args.write_v2_cnf.parent.mkdir(parents=True, exist_ok=True)
        v2.build_base_cnf().cnf.to_file(str(args.write_v2_cnf))
        print(f"wrote {args.write_v2_cnf}")


if __name__ == "__main__":
    main()
