"""Validate and summarize the cross-generation MaxSAT evaluation.

The analysis keeps encoding efficiency, schedule quality, and component
effects separate.  It builds a validated single-run runtime table from the
first completed repetition of every cell, while retaining the three-repetition
campaign as the final robustness analysis.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Sequence

from B2B_Instance import B2BSATModel, read_instance
from Main import collect_instances


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_INSTANCES = 126
EXPECTED_REPETITIONS = 3
EXPECTED_ROWS = 3402
EXPECTED_REUSED_ROWS = 2898
EXPECTED_STRESS_INSTANCES = 100
TIMEOUT_SECONDS = 7200.0

OBJECTIVES = ("bg_d2", "ir", "ir_im_is")
ENCODINGS = ("Direct", "Compact-Base", "Compact-Objective")
CONFIGURATION_MAP = {
    "direct_bg_d2": ("BG-d2", "Direct"),
    "compact_base_bg_d2": ("BG-d2", "Compact-Base"),
    "compact_objective_bg_d2": ("BG-d2", "Compact-Objective"),
    "direct_ir": ("IR", "Direct"),
    "compact_base_ir": ("IR", "Compact-Base"),
    "compact_objective_ir": ("IR", "Compact-Objective"),
    "direct_ir_im_is": ("IR-IM-IS", "Direct"),
    "compact_base_ir_im_is": ("IR-IM-IS", "Compact-Base"),
    "compact_objective_ir_im_is": ("IR-IM-IS", "Compact-Objective"),
}
OBJECTIVE_DISPLAY = {
    "bg_d2": "BG-d2",
    "ir": "IR",
    "ir_im_is": "IR-IM-IS",
}
FORMULA_FIELDS = ("n_vars", "n_total_clauses", "n_total_literals")
QUALITY_FIELDS = (
    "total_break_groups",
    "idle_range_pstar",
    "maximum_internal_idle_slots",
    "total_internal_idle_slots",
)
TERMINAL_STATUSES = {"OPTIMAL", "UNSAT", "TIMEOUT"}
SOURCE_TO_TARGET_CONFIGURATION = {
    "published_2022_bg_d2": "direct_bg_d2",
    "published_2022_ir": "direct_ir",
    "published_2022_ir_im_is": "direct_ir_im_is",
    "bg_reference": "compact_base_bg_d2",
    "ir_reference": "compact_base_ir",
    "irimis_reference": "compact_base_ir_im_is",
    "compact_acf_bg_d2": "compact_objective_bg_d2",
    "compact_cdf_ir": "compact_objective_ir",
    "compact_cdf_ir_im_is": "compact_objective_ir_im_is",
    "conference_ir_rerun": "compact_base_ir",
}

BENCHMARK_ORDER = ("All-126", "Stress", "Stress-high")
BENCHMARK_SIZES = {"All-126": 126, "Stress": 60, "Stress-high": 40}
STRESS_VARIANTS = {
    "Stress": {"prec30", "prec35", "prec40"},
    "Stress-high": {"prec50", "prec60"},
}
PRIMARY_ABLATION_PATHS = {
    "BG-d2": (
        ("bg_reference", "B", None),
        ("bg_a", "B + ZB", "bg_reference"),
        ("bg_c", "B + DS", "bg_reference"),
        ("bg_ac", "B + ZB + DS", "bg_reference"),
        ("bg_acf", "B + ZB + DS + AMO", "bg_ac"),
    ),
    "IR": (
        ("ir_reference", "B", None),
        ("ir_c", "B + DS", "ir_reference"),
        ("ir_d", "B + RC", "ir_reference"),
        ("ir_cd", "B + DS + RC", "ir_reference"),
        ("ir_cdf", "B + DS + RC + AMO", "ir_cd"),
    ),
    "IR-IM-IS": (
        ("irimis_reference", "B", None),
        ("irimis_c", "B + DS", "irimis_reference"),
        ("irimis_d", "B + RC", "irimis_reference"),
        ("irimis_cd", "B + DS + RC", "irimis_reference"),
        ("irimis_cdf", "B + DS + RC + AMO", "irimis_cd"),
    ),
}
CANONICAL_ENDPOINT_SOURCES = {
    "compact_objective_bg_d2": "bg_acf",
    "compact_objective_ir": "ir_cdf",
    "compact_objective_ir_im_is": "irimis_cdf",
}


def read_rows(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as stream:
            rows.extend(csv.DictReader(stream))
    return rows


def write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        if not fieldnames:
            stream.write("")
            return
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def numeric(row: dict[str, str], field: str) -> float:
    value = str(row.get(field, "")).strip()
    if not value:
        raise ValueError(f"missing {field} in {row.get('run_key', 'row')}")
    return float(value)


def population_idle_sd(row: dict[str, str]) -> float:
    """Population SD of participant idle totals for one selected schedule."""
    n = int(float(row.get("objective_participant_count") or 0))
    if n <= 0:
        return 0.0
    total = numeric(row, "total_internal_idle_slots")
    squared = numeric(row, "squared_internal_idle_slots")
    return math.sqrt(max(0.0, squared / n - (total / n) ** 2))


def normalized_vector(row: dict[str, str]) -> str:
    return str(
        row.get("proven_objective_vector")
        or row.get("objective_vector")
        or row.get("objective_value")
        or ""
    ).strip()


def canonical_schedule_row(rows: Sequence[dict[str, str]]) -> dict[str, str]:
    optimal = [row for row in rows if row.get("status") == "OPTIMAL"]
    if not optimal:
        raise ValueError("canonical schedule requires an OPTIMAL result")
    return min(
        optimal,
        key=lambda row: (
            int(row.get("repetition") or 10**9),
            int(row.get("run_order") or 10**9),
        ),
    )


def aggregate_instance_repetitions(
    rows: Sequence[dict[str, str]],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    time_rss_policy: str = "all-runs",
) -> dict[str, object]:
    if not rows or time_rss_policy not in {"all-runs", "optimal-runs"}:
        raise ValueError("nonempty repetitions and a supported time/RSS policy are required")
    statuses = {row.get("status", "") for row in rows}
    if "OPTIMAL" in statuses and "UNSAT" in statuses:
        raise ValueError("conflicting OPTIMAL and UNSAT repetitions")
    if "OPTIMAL" in statuses:
        outcome = "OPTIMAL"
    elif "UNSAT" in statuses:
        outcome = "UNSAT"
    elif statuses == {"TIMEOUT"}:
        outcome = "TIMEOUT"
    else:
        raise ValueError(f"unsupported repetition outcomes: {sorted(statuses)}")

    measured = [row for row in rows if row.get("status") == "OPTIMAL"] \
        if time_rss_policy == "optimal-runs" and outcome == "OPTIMAL" else list(rows)
    runtimes = [numeric(row, "runtime_seconds") for row in measured]
    memories = [numeric(row, "peak_memory_mb") for row in measured]
    par2_values = [
        2.0 * timeout_seconds
        if row.get("status") == "TIMEOUT"
        else numeric(row, "runtime_seconds")
        for row in rows
    ]
    return {
        "outcome": outcome,
        "all_repetitions_optimal": all(row.get("status") == "OPTIMAL" for row in rows),
        "repetitions": len(rows),
        "time_rss_policy": time_rss_policy,
        "time_rss_repetitions": len(measured),
        "optimal_repetitions": sum(row.get("status") == "OPTIMAL" for row in rows),
        "timeout_repetitions": sum(row.get("status") == "TIMEOUT" for row in rows),
        "median_runtime_seconds": median(runtimes),
        "median_par2_seconds": median(par2_values),
        "median_peak_rss_mb": median(memories),
    }


def validate_complete_matrix(
    rows: Sequence[dict[str, str]],
    *,
    expected_instances: int = EXPECTED_INSTANCES,
    expected_repetitions: int = EXPECTED_REPETITIONS,
) -> dict[str, object]:
    errors: list[str] = []
    expected_count = len(CONFIGURATION_MAP) * expected_instances * expected_repetitions
    if len(rows) != expected_count:
        errors.append(f"row_count={len(rows)}, expected={expected_count}")

    keys = [
        (
            row.get("planned_configuration_id", ""),
            row.get("instance_content_id", ""),
            int(row.get("repetition") or 0),
        )
        for row in rows
    ]
    duplicates = [key for key, count in Counter(keys).items() if count != 1]
    if duplicates:
        errors.append(f"duplicate_cells={len(duplicates)}")

    configurations = {row.get("planned_configuration_id", "") for row in rows}
    if configurations != set(CONFIGURATION_MAP):
        errors.append(
            "configuration_ids="
            f"{sorted(configurations)}, expected={sorted(CONFIGURATION_MAP)}"
        )

    content_sha: dict[str, set[str]] = defaultdict(set)
    repetitions: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in rows:
        content_id = row.get("instance_content_id", "")
        content_sha[content_id].add(row.get("instance_sha256", ""))
        repetitions[(row.get("planned_configuration_id", ""), content_id)].add(
            int(row.get("repetition") or 0)
        )
    if len(content_sha) != expected_instances:
        errors.append(f"unique_instances={len(content_sha)}, expected={expected_instances}")
    bad_sha = {key: values for key, values in content_sha.items() if len(values) != 1}
    if bad_sha:
        errors.append(f"content_sha_mismatches={len(bad_sha)}")
    expected_rep_set = set(range(1, expected_repetitions + 1))
    bad_repetitions = {
        key: sorted(values)
        for key, values in repetitions.items()
        if values != expected_rep_set
    }
    if bad_repetitions:
        errors.append(f"incomplete_repetition_cells={len(bad_repetitions)}")

    return {
        "valid": not errors,
        "errors": errors,
        "row_count": len(rows),
        "expected_row_count": expected_count,
        "unique_instance_count": len(content_sha),
        "duplicate_cell_count": len(duplicates),
    }


def formula_invariance_errors(rows: Sequence[dict[str, str]]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row.get("planned_configuration_id", ""),
                row.get("instance_content_id", ""),
            )
        ].append(row)
    errors: list[str] = []
    for key, group in grouped.items():
        signatures = {
            tuple(str(row.get(field, "")) for field in FORMULA_FIELDS)
            for row in group
        }
        if len(signatures) != 1:
            errors.append(f"formula changed across repetitions: {key}")
    return errors


def objective_agreement_errors(rows: Sequence[dict[str, str]]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        configuration = row.get("planned_configuration_id", "")
        if configuration not in CONFIGURATION_MAP:
            continue
        objective, _ = CONFIGURATION_MAP[configuration]
        grouped[(objective, row.get("instance_content_id", ""))].append(row)

    errors: list[str] = []
    for key, group in grouped.items():
        by_encoding: dict[str, set[str]] = defaultdict(set)
        for row in group:
            if row.get("status") != "OPTIMAL":
                continue
            _, encoding = CONFIGURATION_MAP[row["planned_configuration_id"]]
            vector = normalized_vector(row)
            if vector:
                by_encoding[encoding].add(vector)
        if any(len(vectors) > 1 for vectors in by_encoding.values()):
            errors.append(f"objective changed across repetitions: {key}")
            continue
        vectors = {next(iter(values)) for values in by_encoding.values() if values}
        if len(by_encoding) >= 2 and len(vectors) != 1:
            errors.append(f"encoding objective disagreement: {key}")
    return errors


def _formula_from_model(instance_spec, objective: str, encoding: str) -> dict[str, object]:
    if encoding == "Compact-Base":
        compact_encoding = "reference"
        collision_amo = "pairwise"
    elif encoding == "Attainable-Extension":
        compact_encoding = "objective_aware"
        collision_amo = "adaptive_commander"
    else:
        compact_encoding = "optimized"
        collision_amo = "adaptive_commander"

    model = B2BSATModel(
        read_instance(instance_spec.path),
        precedence_encoding="sparse_suffix",
        precedence_graph="distance_closure",
        domain_filter_graph="distance_closure",
        encoding_variant="imp12+",
        domain_mode="reduced",
        objective_mode=objective,
        compact_encoding=compact_encoding,
        collision_amo_encoding=collision_amo,
    )
    artifacts = model.build_base_cnf()
    soft = [
        clause
        for tier in artifacts.objective_tiers
        for clause in tier.maxsat_clauses
    ]
    return {
        "objective": OBJECTIVE_DISPLAY[objective],
        "encoding": encoding,
        "instance": instance_spec.instance_name,
        "instance_content_id": instance_spec.content_id,
        "instance_sha256": instance_spec.sha256,
        "n_vars": artifacts.n_vars,
        "n_total_clauses": artifacts.n_clauses + len(soft),
        "n_total_literals": artifacts.n_hard_literals
        + sum(len(clause) for clause in soft),
    }


def build_formula_audit(
    direct_rows: Sequence[dict[str, str]],
    *,
    manifest: Path,
) -> list[dict[str, object]]:
    direct_configuration = {
        "bg_d2": "published_2022_bg_d2",
        "ir": "published_2022_ir",
        "ir_im_is": "published_2022_ir_im_is",
    }
    direct_index: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in direct_rows:
        for objective, configuration in direct_configuration.items():
            if row.get("planned_configuration_id") == configuration:
                direct_index[(objective, row["instance_content_id"])].append(row)

    instances = collect_instances(None, None, str(manifest.resolve()), "all")
    if len(instances) != EXPECTED_INSTANCES:
        raise ValueError(f"formula audit expected 126 instances, found {len(instances)}")

    output: list[dict[str, object]] = []
    for instance_spec in instances:
        for objective in OBJECTIVES:
            historical = direct_index.get((objective, instance_spec.content_id), [])
            if not historical:
                raise ValueError(
                    f"missing Direct formula for {objective}/{instance_spec.content_id}"
                )
            signatures = {
                tuple(str(row.get(field, "")) for field in FORMULA_FIELDS)
                for row in historical
            }
            if len(signatures) != 1:
                raise ValueError(
                    f"Direct formula changed across repetitions for "
                    f"{objective}/{instance_spec.content_id}"
                )
            representative = historical[0]
            output.append(
                {
                    "objective": OBJECTIVE_DISPLAY[objective],
                    "encoding": "Direct",
                    "instance": instance_spec.instance_name,
                    "instance_content_id": instance_spec.content_id,
                    "instance_sha256": instance_spec.sha256,
                    "n_vars": int(representative["n_vars"]),
                    "n_total_clauses": int(representative["n_total_clauses"]),
                    "n_total_literals": int(representative["n_total_literals"]),
                }
            )
            output.append(_formula_from_model(instance_spec, objective, "Compact-Base"))
            output.append(
                _formula_from_model(instance_spec, objective, "Compact-Objective")
            )
    return output


def build_attainable_extension_audit(*, manifest: Path) -> list[dict[str, object]]:
    instances = collect_instances(None, None, str(manifest.resolve()), "all")
    return [
        _formula_from_model(instance, objective, "Attainable-Extension")
        for instance in instances
        for objective in ("ir", "ir_im_is")
    ]


def formula_tables(
    rows: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    by_instance: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        grouped[(str(row["objective"]), str(row["encoding"]))].append(row)
        by_instance[
            (
                str(row["objective"]),
                str(row["encoding"]),
                str(row["instance_content_id"]),
            )
        ] = row

    summaries: list[dict[str, object]] = []
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        for encoding in ENCODINGS:
            group = grouped[(objective, encoding)]
            summaries.append(
                {
                    "objective": objective,
                    "encoding": encoding,
                    "instances": len(group),
                    "median_variables": median(int(row["n_vars"]) for row in group),
                    "median_clauses": median(
                        int(row["n_total_clauses"]) for row in group
                    ),
                    "median_literals": median(
                        int(row["n_total_literals"]) for row in group
                    ),
                }
            )

    reductions: list[dict[str, object]] = []
    ratios: list[dict[str, object]] = []
    comparisons = (
        ("Compact-Base", "Direct"),
        ("Compact-Objective", "Direct"),
        ("Compact-Objective", "Compact-Base"),
    )
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        instance_ids = sorted(
            str(row["instance_content_id"])
            for row in grouped[(objective, "Direct")]
        )
        for encoding in ("Compact-Base", "Compact-Objective"):
            for instance_id in instance_ids:
                row = by_instance[(objective, encoding, instance_id)]
                direct = by_instance[(objective, "Direct", instance_id)]
                ratios.append(
                    {
                        "objective": objective,
                        "encoding": encoding,
                        "instance_content_id": instance_id,
                        "clause_ratio_to_direct": (
                            int(row["n_total_clauses"])
                            / int(direct["n_total_clauses"])
                        ),
                    }
                )
        for numerator, denominator in comparisons:
            percentages = []
            for instance_id in instance_ids:
                left = int(
                    by_instance[(objective, numerator, instance_id)][
                        "n_total_clauses"
                    ]
                )
                right = int(
                    by_instance[(objective, denominator, instance_id)][
                        "n_total_clauses"
                    ]
                )
                percentages.append(100.0 * (right - left) / right)
            reductions.append(
                {
                    "objective": objective,
                    "encoding": numerator,
                    "baseline": denominator,
                    "paired_median_clause_reduction_percent": median(percentages),
                    "instances_with_fewer_clauses": sum(value > 0 for value in percentages),
                    "instances_equal": sum(value == 0 for value in percentages),
                    "instances_with_more_clauses": sum(value < 0 for value in percentages),
                }
            )
    return summaries, reductions, ratios


def formula_effect_rows(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Return per-instance clause reductions for every matched encoding pair."""

    by_instance = {
        (
            str(row["objective"]),
            str(row["encoding"]),
            str(row["instance_content_id"]),
        ): row
        for row in rows
    }
    effects: list[dict[str, object]] = []
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        instance_ids = sorted(
            key[2]
            for key in by_instance
            if key[0] == objective and key[1] == "Direct"
        )
        for left, right in (
            ("Direct", "Compact-Base"),
            ("Direct", "Compact-Objective"),
            ("Compact-Base", "Compact-Objective"),
        ):
            for instance_id in instance_ids:
                left_clauses = int(
                    by_instance[(objective, left, instance_id)]["n_total_clauses"]
                )
                right_clauses = int(
                    by_instance[(objective, right, instance_id)]["n_total_clauses"]
                )
                effects.append(
                    {
                        "objective": objective,
                        "left_encoding": left,
                        "right_encoding": right,
                        "instance_content_id": instance_id,
                        "clause_reduction_percent": 100.0
                        * (left_clauses - right_clauses)
                        / left_clauses,
                    }
                )
    return effects


def stress_dataset(row: dict[str, object] | dict[str, str]) -> str:
    """Return the reader-facing stress-set name for a derived instance."""

    variant = str(row.get("instance_variant", ""))
    for dataset, variants in STRESS_VARIANTS.items():
        if variant in variants:
            return dataset
    raise ValueError(f"unsupported stress variant: {variant or 'missing'}")


def formula_rows_from_mapped_runs(
    rows: Sequence[dict[str, str]],
) -> list[dict[str, object]]:
    """Project validated matched-run rows onto deterministic formula measures."""

    projected: list[dict[str, object]] = []
    for row in rows:
        configuration = str(row.get("planned_configuration_id", ""))
        if configuration not in CONFIGURATION_MAP:
            raise ValueError(f"unknown matched configuration: {configuration}")
        objective, encoding = CONFIGURATION_MAP[configuration]
        projected.append(
            {
                "objective": objective,
                "encoding": encoding,
                "instance": row.get("instance", ""),
                "instance_content_id": row.get("instance_content_id", ""),
                "instance_sha256": row.get("instance_sha256", ""),
                "instance_variant": row.get("instance_variant", ""),
                "n_vars": int(float(row["n_vars"])),
                "n_total_clauses": int(float(row["n_total_clauses"])),
                "n_total_literals": int(float(row["n_total_literals"])),
            }
        )
    return projected


def benchmark_formula_tables(
    official_rows: Sequence[dict[str, object]],
    stress_rows: Sequence[dict[str, str]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Summarize formula size for All-126, Stress, and Stress-high."""

    summaries: list[dict[str, object]] = []
    reductions: list[dict[str, object]] = []
    effects: list[dict[str, object]] = []
    stress_formula_rows = formula_rows_from_mapped_runs(stress_rows)
    groups: list[tuple[str, list[dict[str, object]]]] = [
        ("All-126", list(official_rows)),
        *[
            (
                dataset,
                [
                    row
                    for row in stress_formula_rows
                    if stress_dataset(row) == dataset
                ],
            )
            for dataset in ("Stress", "Stress-high")
        ],
    ]
    for dataset, rows in groups:
        expected = BENCHMARK_SIZES[dataset] * len(CONFIGURATION_MAP)
        if len(rows) != expected:
            raise ValueError(
                f"{dataset} formula matrix has {len(rows)} rows; expected {expected}"
            )
        dataset_summary, dataset_reductions, _ = formula_tables(rows)
        dataset_effects = formula_effect_rows(rows)
        for collection in (dataset_summary, dataset_reductions, dataset_effects):
            for row in collection:
                row["dataset"] = dataset
                row["dataset_instances"] = BENCHMARK_SIZES[dataset]
        summaries.extend(dataset_summary)
        reductions.extend(dataset_reductions)
        effects.extend(dataset_effects)
    return summaries, reductions, effects


def runtime_tables(
    rows: Sequence[dict[str, str]],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    time_rss_policy: str = "all-runs",
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row["planned_configuration_id"],
                row["instance_content_id"],
            )
        ].append(row)

    collapsed: dict[tuple[str, str], dict[str, object]] = {
        key: aggregate_instance_repetitions(group, timeout_seconds=timeout_seconds,
                                            time_rss_policy=time_rss_policy)
        for key, group in grouped.items()
    }
    summaries: list[dict[str, object]] = []
    timeouts: list[dict[str, object]] = []
    for configuration, (objective, encoding) in CONFIGURATION_MAP.items():
        cells = [
            value
            for (observed_configuration, _), value in collapsed.items()
            if observed_configuration == configuration
        ]
        counts = Counter(str(cell["outcome"]) for cell in cells)
        summaries.append(
            {
                "objective": objective,
                "encoding": encoding,
                "OPT": counts["OPTIMAL"],
                "UNSAT": counts["UNSAT"],
                "timeout": counts["TIMEOUT"],
                "median_runtime_seconds": median(
                    float(cell["median_runtime_seconds"]) for cell in cells
                ),
                "mean_PAR2_seconds": mean(
                    float(cell["median_par2_seconds"]) for cell in cells
                ),
                "median_peak_RSS_mb": median(
                    float(cell["median_peak_rss_mb"]) for cell in cells
                ),
            }
        )
        for (observed_configuration, instance_id), cell in collapsed.items():
            if observed_configuration == configuration and cell["outcome"] == "TIMEOUT":
                timeouts.append(
                    {
                        "objective": objective,
                        "encoding": encoding,
                        "instance_content_id": instance_id,
                    }
                )

    ratios: list[dict[str, object]] = []
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        objective_configs = {
            encoding: configuration
            for configuration, (observed_objective, encoding) in CONFIGURATION_MAP.items()
            if observed_objective == objective
        }
        for left, right in (
            ("Compact-Base", "Direct"),
            ("Compact-Objective", "Direct"),
            ("Compact-Objective", "Compact-Base"),
        ):
            values = []
            for instance_id in {
                key[1]
                for key in collapsed
                if key[0] == objective_configs[left]
            }:
                left_cell = collapsed[(objective_configs[left], instance_id)]
                right_cell = collapsed[(objective_configs[right], instance_id)]
                if left_cell["all_repetitions_optimal"] and right_cell["all_repetitions_optimal"]:
                    values.append(
                        float(left_cell["median_runtime_seconds"])
                        / float(right_cell["median_runtime_seconds"])
                    )
            ratios.append(
                {
                    "objective": objective,
                    "encoding": left,
                    "baseline": right,
                    "paired_OPT_instances": len(values),
                    "paired_median_runtime_ratio": median(values) if values else "",
                }
            )
    return summaries, ratios, timeouts


def runtime_effect_rows(
    rows: Sequence[dict[str, str]],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    """Return per-instance speed ratios for pairs that both prove optimality."""

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["planned_configuration_id"], row["instance_content_id"])].append(
            row
        )
    collapsed = {
        key: aggregate_instance_repetitions(group, timeout_seconds=timeout_seconds)
        for key, group in grouped.items()
    }

    effects: list[dict[str, object]] = []
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        objective_configs = {
            encoding: configuration
            for configuration, (observed_objective, encoding) in CONFIGURATION_MAP.items()
            if observed_objective == objective
        }
        for left, right in (
            ("Direct", "Compact-Base"),
            ("Direct", "Compact-Objective"),
            ("Compact-Base", "Compact-Objective"),
        ):
            left_configuration = objective_configs[left]
            right_configuration = objective_configs[right]
            instance_ids = sorted(
                instance_id
                for configuration, instance_id in collapsed
                if configuration == left_configuration
            )
            for instance_id in instance_ids:
                left_cell = collapsed[(left_configuration, instance_id)]
                right_cell = collapsed[(right_configuration, instance_id)]
                if not left_cell["all_repetitions_optimal"] or not right_cell["all_repetitions_optimal"]:
                    continue
                effects.append(
                    {
                        "objective": objective,
                        "left_encoding": left,
                        "right_encoding": right,
                        "instance_content_id": instance_id,
                        "runtime_speed_ratio_left_over_right": float(
                            left_cell["median_runtime_seconds"]
                        )
                        / float(right_cell["median_runtime_seconds"]),
                    }
                )
    return effects


def benchmark_runtime_tables(
    official_rows: Sequence[dict[str, str]],
    stress_rows: Sequence[dict[str, str]],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    expected_repetitions: int = 1,
    time_rss_policy: str = "all-runs",
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Summarize solver effort under the same metric contract on all sets."""

    summaries: list[dict[str, object]] = []
    ratios: list[dict[str, object]] = []
    effects: list[dict[str, object]] = []
    timeouts: list[dict[str, object]] = []
    groups: list[tuple[str, list[dict[str, str]]]] = [
        ("All-126", list(official_rows)),
        *[
            (
                dataset,
                [row for row in stress_rows if stress_dataset(row) == dataset],
            )
            for dataset in ("Stress", "Stress-high")
        ],
    ]
    for dataset, rows in groups:
        expected = BENCHMARK_SIZES[dataset] * len(CONFIGURATION_MAP) * expected_repetitions
        if len(rows) != expected:
            raise ValueError(
                f"{dataset} runtime matrix has {len(rows)} rows; expected {expected}"
            )
        dataset_summary, dataset_ratios, dataset_timeouts = runtime_tables(
            rows,
            timeout_seconds=timeout_seconds,
            time_rss_policy=time_rss_policy,
        )
        dataset_effects = runtime_effect_rows(
            rows,
            timeout_seconds=timeout_seconds,
        )
        for collection in (
            dataset_summary,
            dataset_ratios,
            dataset_effects,
            dataset_timeouts,
        ):
            for row in collection:
                row["dataset"] = dataset
                row["dataset_instances"] = BENCHMARK_SIZES[dataset]
        summaries.extend(dataset_summary)
        ratios.extend(dataset_ratios)
        effects.extend(dataset_effects)
        timeouts.extend(dataset_timeouts)
    return summaries, ratios, effects, timeouts


def compare_rows(
    left: dict[str, dict[str, str]],
    right: dict[str, dict[str, str]],
    keys: Sequence[str],
    field: str,
) -> dict[str, int]:
    differences = [numeric(left[key], field) - numeric(right[key], field) for key in keys]
    return {
        "lower": sum(value < 0 for value in differences),
        "equal": sum(value == 0 for value in differences),
        "higher": sum(value > 0 for value in differences),
    }


def compare_quality_rows(
    left: dict[str, dict[str, str]],
    right: dict[str, dict[str, str]],
    keys: Sequence[str],
    field: str,
) -> dict[str, int]:
    value = population_idle_sd if field == "population_sd_idle_slots" else (
        lambda row: numeric(row, field)
    )
    differences = [value(left[key]) - value(right[key]) for key in keys]
    return {
        "lower": sum(item < 0 for item in differences),
        "equal": sum(item == 0 for item in differences),
        "higher": sum(item > 0 for item in differences),
    }


def quality_tables(
    campaign_rows: Sequence[dict[str, str]],
    bg_rows: Sequence[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    selected_configs = {
        "IR": "compact_objective_ir",
        "IR-IM-IS": "compact_objective_ir_im_is",
    }
    grouped: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in campaign_rows:
        for objective, configuration in selected_configs.items():
            if row.get("planned_configuration_id") == configuration:
                grouped[objective][row["instance_content_id"]].append(row)
    grouped["BG-dinf"] = defaultdict(list)
    for row in bg_rows:
        grouped["BG-dinf"][row["instance_content_id"]].append(row)

    selected: dict[str, dict[str, dict[str, str]]] = {}
    for objective, instances in grouped.items():
        selected[objective] = {
            instance_id: canonical_schedule_row(rows)
            for instance_id, rows in instances.items()
            if any(row.get("status") == "OPTIMAL" for row in rows)
        }
    analysis_set = sorted(set.intersection(*(set(rows) for rows in selected.values())))
    medians: list[dict[str, object]] = []
    for objective in ("BG-dinf", "IR", "IR-IM-IS"):
        medians.append(
            {
                "objective": objective,
                "instances": len(analysis_set),
                **{
                    f"median_{field}": median(
                        numeric(selected[objective][key], field)
                        for key in analysis_set
                    )
                    for field in QUALITY_FIELDS
                },
                "median_population_sd_idle_slots": median(
                    population_idle_sd(selected[objective][key])
                    for key in analysis_set
                ),
            }
        )

    comparisons: list[dict[str, object]] = []
    effects: list[dict[str, object]] = []
    for left, right, fields in (
        ("IR-IM-IS", "IR", ("maximum_internal_idle_slots", "total_internal_idle_slots", "population_sd_idle_slots")),
        ("BG-dinf", "IR", ("total_break_groups", "total_internal_idle_slots", "population_sd_idle_slots")),
        ("BG-dinf", "IR-IM-IS", ("total_break_groups", "total_internal_idle_slots", "population_sd_idle_slots")),
    ):
        for field in fields:
            counts = compare_quality_rows(selected[left], selected[right], analysis_set, field)
            comparisons.append(
                {
                    "left_objective": left,
                    "right_objective": right,
                    "measure": field,
                    **counts,
                }
            )
            for instance_id in analysis_set:
                left_value = (
                    population_idle_sd(selected[left][instance_id])
                    if field == "population_sd_idle_slots"
                    else numeric(selected[left][instance_id], field)
                )
                right_value = (
                    population_idle_sd(selected[right][instance_id])
                    if field == "population_sd_idle_slots"
                    else numeric(selected[right][instance_id], field)
                )
                effects.append(
                    {
                        "left_objective": left,
                        "right_objective": right,
                        "measure": field,
                        "instance_content_id": instance_id,
                        "difference": left_value - right_value,
                    }
                )
    membership = [
        {
            "analysis": "three_objective_schedule_quality",
            "instance_content_id": key,
            "included": key in analysis_set,
        }
        for key in sorted(set.union(*(set(rows) for rows in selected.values())))
    ]
    return medians, comparisons, membership, effects


def stress_quality_tables(
    rows: Sequence[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Compare IR-IM-IS with IR on each precedence-stress set."""

    target_configs = {
        "IR": "compact_objective_ir",
        "IR-IM-IS": "compact_objective_ir_im_is",
    }
    comparisons: list[dict[str, object]] = []
    membership: list[dict[str, object]] = []
    for dataset in ("Stress", "Stress-high"):
        selected: dict[str, dict[str, dict[str, str]]] = {}
        for objective, configuration in target_configs.items():
            selected[objective] = {
                row["instance_content_id"]: row
                for row in rows
                if row.get("planned_configuration_id") == configuration
                and stress_dataset(row) == dataset
                and row.get("status") == "OPTIMAL"
            }
        analysis_set = sorted(set(selected["IR"]) & set(selected["IR-IM-IS"]))
        for field in (
            "idle_range_pstar",
            "maximum_internal_idle_slots",
            "total_internal_idle_slots",
        ):
            counts = compare_rows(
                selected["IR-IM-IS"],
                selected["IR"],
                analysis_set,
                field,
            )
            comparisons.append(
                {
                    "dataset": dataset,
                    "instances": len(analysis_set),
                    "left_objective": "IR-IM-IS",
                    "right_objective": "IR",
                    "measure": field,
                    **counts,
                }
            )
        all_ids = sorted(set(selected["IR"]) | set(selected["IR-IM-IS"]))
        membership.extend(
            {
                "analysis": "stress_waiting_objective_quality",
                "dataset": dataset,
                "instance_content_id": instance_id,
                "included": instance_id in analysis_set,
            }
            for instance_id in all_ids
        )
    return comparisons, membership


def canonical_primary_ablation_rows(
    ablation_rows: Sequence[dict[str, str]],
    matched_rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Use one canonical endpoint in RQ1 and the component study."""

    configurations = {
        configuration
        for steps in PRIMARY_ABLATION_PATHS.values()
        for configuration, _, _ in steps
    }
    endpoint_configurations = set(CANONICAL_ENDPOINT_SOURCES.values())
    selected: list[dict[str, str]] = [
        dict(row)
        for row in ablation_rows
        if row.get("planned_configuration_id") in configurations
        and row.get("planned_configuration_id") not in endpoint_configurations
        and int(row.get("repetition") or 0) == 1
    ]
    for row in matched_rows:
        source_configuration = CANONICAL_ENDPOINT_SOURCES.get(
            str(row.get("planned_configuration_id", ""))
        )
        if source_configuration is None:
            continue
        copied = dict(row)
        copied["planned_configuration_id"] = source_configuration
        selected.append(copied)

    invalid = [
        f"{row.get('run_key', 'row')}: {';'.join(reuse_row_errors(row))}"
        for row in selected
        if reuse_row_errors(row)
    ]
    if invalid:
        raise ValueError("invalid canonical ablation rows: " + "; ".join(invalid[:5]))
    keys = [
        (row["planned_configuration_id"], row["instance_content_id"])
        for row in selected
    ]
    duplicates = [key for key, count in Counter(keys).items() if count != 1]
    if duplicates:
        raise ValueError(f"duplicate canonical ablation cells: {len(duplicates)}")
    return selected


def primary_ablation_summary(
    rows: Sequence[dict[str, str]],
    *,
    datasets: Sequence[str],
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    """Report structural and solver metrics for the primary component paths."""

    summaries: list[dict[str, object]] = []
    for dataset in datasets:
        dataset_rows = (
            list(rows)
            if dataset == "All-126"
            else [row for row in rows if stress_dataset(row) == dataset]
        )
        expected_instances = BENCHMARK_SIZES[dataset]
        for objective, steps in PRIMARY_ABLATION_PATHS.items():
            groups = {
                configuration: [
                    row
                    for row in dataset_rows
                    if row.get("planned_configuration_id") == configuration
                ]
                for configuration, _, _ in steps
            }
            for configuration, symbol, baseline_configuration in steps:
                group = groups[configuration]
                if len(group) != expected_instances:
                    raise ValueError(
                        f"{dataset}/{configuration} has {len(group)} rows; "
                        f"expected {expected_instances}"
                    )
                counts = Counter(row.get("status", "") for row in group)
                par2 = [
                    2.0 * timeout_seconds
                    if row.get("status") == "TIMEOUT"
                    else numeric(row, "runtime_seconds")
                    for row in group
                ]
                reduction: object = ""
                if baseline_configuration is not None:
                    baseline = {
                        row["instance_content_id"]: row
                        for row in groups[baseline_configuration]
                    }
                    candidate = {
                        row["instance_content_id"]: row for row in group
                    }
                    if set(baseline) != set(candidate):
                        raise ValueError(
                            f"{dataset}/{configuration} has a mismatched baseline set"
                        )
                    reduction = median(
                        100.0
                        * (
                            numeric(baseline[key], "n_total_clauses")
                            - numeric(candidate[key], "n_total_clauses")
                        )
                        / numeric(baseline[key], "n_total_clauses")
                        for key in baseline
                    )
                summaries.append(
                    {
                        "dataset": dataset,
                        "dataset_instances": expected_instances,
                        "objective": objective,
                        "configuration": symbol,
                        "source_configuration": configuration,
                        "median_variables": median(
                            numeric(row, "n_vars") for row in group
                        ),
                        "median_clauses": median(
                            numeric(row, "n_total_clauses") for row in group
                        ),
                        "median_literals": median(
                            numeric(row, "n_total_literals") for row in group
                        ),
                        "paired_median_clause_reduction_percent": reduction,
                        "OPT": counts["OPTIMAL"],
                        "UNSAT": counts["UNSAT"],
                        "timeout": counts["TIMEOUT"],
                        "solved": counts["OPTIMAL"] + counts["UNSAT"],
                        "median_runtime_seconds": median(
                            numeric(row, "runtime_seconds") for row in group
                        ),
                        "mean_PAR2_seconds": mean(par2),
                        "median_peak_RSS_mb": median(
                            numeric(row, "peak_memory_mb") for row in group
                        ),
                    }
                )
    return summaries


def endpoint_formula_equivalence_errors(
    ablation_rows: Sequence[dict[str, str]],
    matched_rows: Sequence[dict[str, str]],
) -> list[str]:
    """Check that duplicate endpoint builds have identical deterministic size."""

    errors: list[str] = []
    for matched_configuration, ablation_configuration in (
        ("compact_objective_bg_d2", "bg_acf"),
        ("compact_objective_ir", "ir_cdf"),
        ("compact_objective_ir_im_is", "irimis_cdf"),
    ):
        matched = {
            row["instance_content_id"]: row
            for row in matched_rows
            if row.get("planned_configuration_id") == matched_configuration
        }
        ablation = {
            row["instance_content_id"]: row
            for row in ablation_rows
            if row.get("planned_configuration_id") == ablation_configuration
        }
        if set(matched) != set(ablation):
            errors.append(
                f"{matched_configuration}/{ablation_configuration}: "
                "different instance sets"
            )
            continue
        for instance_id in sorted(matched):
            if matched[instance_id].get("instance_sha256") != ablation[instance_id].get(
                "instance_sha256"
            ):
                errors.append(f"{ablation_configuration}/{instance_id}: SHA-256 mismatch")
                continue
            for field in FORMULA_FIELDS:
                if int(float(matched[instance_id][field])) != int(
                    float(ablation[instance_id][field])
                ):
                    errors.append(
                        f"{ablation_configuration}/{instance_id}: {field} mismatch"
                    )
    return errors


OBJECTIVE_TEX = {
    "BG-d2": r"\textsc{BG-d2}",
    "IR": r"\textsc{IR}",
    "IR-IM-IS": r"\textsc{IR-IM-IS}",
}
ENCODING_TEX = {
    "Direct": r"\textsc{Direct}",
    "Compact-Base": r"\textsc{Compact-Base}",
    "Compact-Objective": r"\textsc{Compact-Objective}",
}


def _tex_number(value: object, *, decimals: int = 1) -> str:
    number = float(value)
    if decimals == 0 or number.is_integer():
        return f"{int(round(number)):,}".replace(",", r"{,}")
    return f"{number:,.{decimals}f}".replace(",", r"{,}")


def _tex_bold(value: str, enabled: bool) -> str:
    return rf"\textbf{{{value}}}" if enabled else value


def _tex_component_cells(
    configuration: object,
    components: Sequence[str] = ("B", "ZB", "DS", "RC", "AMO"),
) -> str:
    enabled = {token.strip() for token in str(configuration).split("+")}
    return " & ".join(
        r"\tablecheck" if component in enabled else "" for component in components
    )


def _write_macro(lines: list[str], name: str, rows: Sequence[str]) -> None:
    lines.append(rf"\newcommand{{\{name}}}{{%")
    lines.extend(f"  {row}%" for row in rows)
    lines.append("}")
    lines.append("")


def write_latex_result_rows(
    path: Path,
    *,
    formula_summary: Sequence[dict[str, object]],
    formula_reductions: Sequence[dict[str, object]],
    runtime_summary: Sequence[dict[str, object]],
    runtime_ratios: Sequence[dict[str, object]],
    runtime_effects: Sequence[dict[str, object]],
    stress_quality: Sequence[dict[str, object]],
    ablation_summary: Sequence[dict[str, object]],
    quality_summary: Sequence[dict[str, object]] = (),
    quality_pairs: Sequence[dict[str, object]] = (),
    aggregation_description: str = "the validated first run for each experimental cell",
) -> None:
    """Write table rows from validated CSV summaries for the manuscript."""

    lines = [
        "% Generated by src/Cross_Generation_Analysis.py; do not edit by hand.",
        f"% Solver values use {aggregation_description}.",
        "",
    ]
    if quality_summary:
        _write_macro(lines, "MainQualityN", [str(quality_summary[0]["instances"])])
        quality_fields = (
            "median_total_break_groups", "median_idle_range_pstar",
            "median_maximum_internal_idle_slots", "median_total_internal_idle_slots",
            "median_population_sd_idle_slots",
        )
        quality_minima = {
            field: min(float(row[field]) for row in quality_summary)
            for field in quality_fields
        }
        median_rows = []
        for row in quality_summary:
            objective = (r"\textsc{BG-d$\infty$}" if row["objective"] == "BG-dinf"
                         else OBJECTIVE_TEX[str(row["objective"])])
            values = [
                _tex_bold(
                    f"{float(row[field]):,.{3 if field == 'median_population_sd_idle_slots' else 1}f}".replace(",", "{,}"),
                    float(row[field]) == quality_minima[field],
                )
                for field in quality_fields
            ]
            median_rows.append(objective + " & " + " & ".join(values) + r" \\")
        _write_macro(lines, "MainQualityMedianRows", median_rows)
    for metric, macro in (("maximum_internal_idle_slots", "MainQualityMaximumLower"),
                          ("total_internal_idle_slots", "MainQualityTotalLower")):
        matching = [row for row in quality_pairs if row["left_objective"] == "IR-IM-IS"
                    and row["right_objective"] == "IR" and row["measure"] == metric]
        if matching:
            _write_macro(lines, macro, [str(matching[0]["lower"])])
    for dataset, prefix in (("Stress", "StressQuality"), ("Stress-high", "StressHighQuality")):
        matching = [row for row in stress_quality if row["dataset"] == dataset
                    and row["measure"] == "total_internal_idle_slots"]
        if matching:
            _write_macro(lines, prefix + "N", [str(matching[0]["instances"])])
            _write_macro(lines, prefix + "TotalLower", [str(matching[0]["lower"])])

    formula_index = {
        (str(row["dataset"]), str(row["objective"]), str(row["encoding"])): row
        for row in formula_summary
    }
    formula_rows: list[str] = []
    for dataset in BENCHMARK_ORDER:
        formula_rows.append(
            rf"\multicolumn{{4}}{{@{{}}l}}{{\textit{{{dataset} ($n={BENCHMARK_SIZES[dataset]}$)}}}} \\"
        )
        for encoding in ENCODINGS:
            values = [
                float(formula_index[(dataset, objective, encoding)]["median_clauses"])
                for objective in ("BG-d2", "IR", "IR-IM-IS")
            ]
            cells = []
            for objective, value in zip(("BG-d2", "IR", "IR-IM-IS"), values):
                best = min(
                    float(formula_index[(dataset, objective, candidate)]["median_clauses"])
                    for candidate in ENCODINGS
                )
                cells.append(_tex_bold(_tex_number(value), value == best))
            formula_rows.append(
                f"{ENCODING_TEX[encoding]} & " + " & ".join(cells) + r" \\"
            )
        if dataset != BENCHMARK_ORDER[-1]:
            formula_rows.append(r"\addlinespace")
    _write_macro(lines, "MatchedFormulaRows", formula_rows)

    runtime_index = {
        (str(row["dataset"]), str(row["objective"]), str(row["encoding"])): row
        for row in runtime_summary
    }
    primary_runtime_rows: list[str] = []
    for dataset in BENCHMARK_ORDER:
        primary_runtime_rows.append(
            rf"\multicolumn{{7}}{{@{{}}l}}{{\textit{{{dataset} ($n={BENCHMARK_SIZES[dataset]}$)}}}} \\"
        )
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            direct = runtime_index[(dataset, objective, "Direct")]
            proposed = runtime_index[(dataset, objective, "Compact-Objective")]
            for row in (direct, proposed):
                if sum(int(row[field]) for field in ("OPT", "UNSAT", "timeout")) != BENCHMARK_SIZES[dataset]:
                    raise ValueError(f"outcome counts do not cover {dataset}/{objective}")
            minima = {
                field: min(float(row[field]) for row in (direct, proposed))
                for field in ("mean_PAR2_seconds", "median_peak_RSS_mb")
            }
            optimum_counts_differ = int(direct["OPT"]) != int(proposed["OPT"])
            best_opt = max(int(direct["OPT"]), int(proposed["OPT"]))
            cells = []
            for row in (direct, proposed):
                opt = _tex_bold(
                    str(int(row["OPT"])),
                    optimum_counts_differ and int(row["OPT"]) == best_opt,
                )
                cells.append(f"{opt}/{int(row['UNSAT'])}/{int(row['timeout'])}")
                cells.extend(
                    _tex_bold(_tex_number(row[field], decimals=1), float(row[field]) == minima[field])
                    for field in ("mean_PAR2_seconds", "median_peak_RSS_mb")
                )
            primary_runtime_rows.append(
                " & ".join([OBJECTIVE_TEX[objective], *cells])
                + r" \\"
            )
        if dataset != BENCHMARK_ORDER[-1]:
            primary_runtime_rows.append(r"\addlinespace")
    _write_macro(lines, "MatchedRuntimePrimaryRows", primary_runtime_rows)

    formula_detail_rows: list[str] = []
    for dataset in BENCHMARK_ORDER:
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            for position, encoding in enumerate(ENCODINGS):
                row = formula_index[(dataset, objective, encoding)]
                formula_detail_rows.append(
                    " & ".join(
                        (
                            dataset if objective == "BG-d2" and position == 0 else "",
                            OBJECTIVE_TEX[objective] if position == 0 else "",
                            ENCODING_TEX[encoding],
                            _tex_number(row["median_variables"]),
                            _tex_number(row["median_clauses"]),
                            _tex_number(row["median_literals"]),
                        )
                    )
                    + r" \\"
                )
            if objective != "IR-IM-IS":
                formula_detail_rows.append(r"\addlinespace[1pt]")
        if dataset != BENCHMARK_ORDER[-1]:
            formula_detail_rows.append(r"\addlinespace")
    _write_macro(lines, "MatchedFormulaDetailRows", formula_detail_rows)

    runtime_rows_by_dataset: dict[str, list[str]] = {}
    for dataset in BENCHMARK_ORDER:
        runtime_rows: list[str] = [
            rf"\multicolumn{{8}}{{@{{}}l}}{{\textit{{{dataset} ($n={BENCHMARK_SIZES[dataset]}$)}}}} \\"
        ]
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            group = [runtime_index[(dataset, objective, encoding)] for encoding in ENCODINGS]
            best = {
                "OPT": max(int(row["OPT"]) for row in group),
                "timeout": min(int(row["timeout"]) for row in group),
                "median_runtime_seconds": min(
                    float(row["median_runtime_seconds"]) for row in group
                ),
                "mean_PAR2_seconds": min(float(row["mean_PAR2_seconds"]) for row in group),
                "median_peak_RSS_mb": min(float(row["median_peak_RSS_mb"]) for row in group),
            }
            for position, encoding in enumerate(ENCODINGS):
                row = runtime_index[(dataset, objective, encoding)]
                cells = [
                    _tex_bold(str(row["OPT"]), int(row["OPT"]) == best["OPT"]),
                    str(row["UNSAT"]),
                    _tex_bold(
                        str(row["timeout"]), int(row["timeout"]) == best["timeout"]
                    ),
                    _tex_bold(
                        _tex_number(row["median_runtime_seconds"], decimals=3),
                        float(row["median_runtime_seconds"])
                        == best["median_runtime_seconds"],
                    ),
                    _tex_bold(
                        _tex_number(row["mean_PAR2_seconds"], decimals=3),
                        float(row["mean_PAR2_seconds"]) == best["mean_PAR2_seconds"],
                    ),
                    _tex_bold(
                        _tex_number(row["median_peak_RSS_mb"], decimals=2),
                        float(row["median_peak_RSS_mb"]) == best["median_peak_RSS_mb"],
                    ),
                ]
                objective_cell = OBJECTIVE_TEX[objective] if position == 0 else ""
                runtime_rows.append(
                    f"{objective_cell} & {ENCODING_TEX[encoding]} & "
                    + " & ".join(cells)
                    + r" \\"
                )
            if objective != "IR-IM-IS":
                runtime_rows.append(r"\addlinespace[1pt]")
        runtime_rows_by_dataset[dataset] = runtime_rows
    _write_macro(
        lines,
        "MatchedRuntimeAllRows",
        runtime_rows_by_dataset["All-126"],
    )
    _write_macro(
        lines,
        "MatchedRuntimeStressRows",
        runtime_rows_by_dataset["Stress"]
        + [r"\addlinespace"]
        + runtime_rows_by_dataset["Stress-high"],
    )

    measure_tex = {
        "idle_range_pstar": r"$\Delta_I$",
        "maximum_internal_idle_slots": r"$I_{\max}$",
        "total_internal_idle_slots": r"$I_\Sigma$",
    }
    quality_rows: list[str] = []
    for dataset in ("Stress", "Stress-high"):
        group = [row for row in stress_quality if row["dataset"] == dataset]
        for position, row in enumerate(group):
            quality_rows.append(
                " & ".join(
                    (
                        dataset if position == 0 else "",
                        str(row["instances"]),
                        measure_tex[str(row["measure"])],
                        str(row["lower"]),
                        str(row["equal"]),
                        str(row["higher"]),
                    )
                )
                + r" \\"
            )
        if dataset == "Stress":
            quality_rows.append(r"\addlinespace")
    _write_macro(lines, "StressQualityRows", quality_rows)

    all126_ablation = [
        row for row in ablation_summary if row["dataset"] == "All-126"
    ]
    ablation_runtime_rows: list[str] = []
    for objective in ("BG-d2", "IR", "IR-IM-IS"):
        group = [row for row in all126_ablation if row["objective"] == objective]
        best_solved = max(int(row["solved"]) for row in group)
        best_runtime = min(float(row["median_runtime_seconds"]) for row in group)
        best_par2 = min(float(row["mean_PAR2_seconds"]) for row in group)
        best_rss = min(float(row["median_peak_RSS_mb"]) for row in group)
        for position, row in enumerate(group):
            ablation_runtime_rows.append(
                f"{OBJECTIVE_TEX[objective] if position == 0 else ''} & "
                f"{_tex_component_cells(row['configuration'])} & "
                + " & ".join(
                    (
                        _tex_bold(str(row["solved"]), int(row["solved"]) == best_solved),
                        _tex_bold(
                            _tex_number(row["median_runtime_seconds"], decimals=3),
                            float(row["median_runtime_seconds"]) == best_runtime,
                        ),
                        _tex_bold(
                            _tex_number(row["mean_PAR2_seconds"], decimals=3),
                            float(row["mean_PAR2_seconds"]) == best_par2,
                        ),
                        _tex_bold(
                            _tex_number(row["median_peak_RSS_mb"], decimals=2),
                            float(row["median_peak_RSS_mb"]) == best_rss,
                        ),
                    )
                )
                + r" \\"
            )
        if objective != "IR-IM-IS":
            ablation_runtime_rows.append(r"\addlinespace")
    _write_macro(lines, "AblationRuntimeRows", ablation_runtime_rows)

    reduction_index = {
        (
            str(row["dataset"]),
            str(row["objective"]),
            str(row["baseline"]),
            str(row["encoding"]),
        ): row
        for row in formula_reductions
    }
    ratio_index = {
        (
            str(row["dataset"]),
            str(row["objective"]),
            str(row["baseline"]),
            str(row["encoding"]),
        ): row
        for row in runtime_ratios
    }
    effect_groups: dict[
        tuple[str, str, str, str], list[dict[str, object]]
    ] = defaultdict(list)
    for row in runtime_effects:
        effect_groups[
            (
                str(row["dataset"]),
                str(row["objective"]),
                str(row["left_encoding"]),
                str(row["right_encoding"]),
            )
        ].append(row)
    effect_rows: list[str] = []
    transitions = (
        ("Direct", "Compact-Base"),
        ("Direct", "Compact-Objective"),
        ("Compact-Base", "Compact-Objective"),
    )
    for dataset in ("Stress", "Stress-high"):
        effect_rows.append(
            rf"\multicolumn{{6}}{{@{{}}l}}{{\textit{{{dataset} ($n={BENCHMARK_SIZES[dataset]}$)}}}} \\"
        )
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            for position, (left, right) in enumerate(transitions):
                reduction = reduction_index[(dataset, objective, left, right)]
                ratio = ratio_index[(dataset, objective, left, right)]
                paired_effects = effect_groups[(dataset, objective, left, right)]
                speed_ratio = median(
                    float(row["runtime_speed_ratio_left_over_right"])
                    for row in paired_effects
                )
                if len(paired_effects) != int(ratio["paired_OPT_instances"]):
                    raise ValueError(
                        f"runtime effect count mismatch for {dataset}/{objective}/"
                        f"{left}/{right}"
                    )
                effect_rows.append(
                    f"{OBJECTIVE_TEX[objective] if position == 0 else ''} & "
                    f"{ENCODING_TEX[left]} $\\rightarrow$ {ENCODING_TEX[right]} & "
                    f"{_tex_number(reduction['paired_median_clause_reduction_percent'], decimals=2)} & "
                    f"{ratio['paired_OPT_instances']} & "
                    f"{_tex_number(speed_ratio, decimals=2)}$\\times$ & "
                    f"{reduction['instances_with_fewer_clauses']}/{reduction['instances_equal']}/{reduction['instances_with_more_clauses']}"
                    + r" \\"
                )
            if objective != "IR-IM-IS":
                effect_rows.append(r"\addlinespace[1pt]")
        if dataset == "Stress":
            effect_rows.append(r"\addlinespace")
    _write_macro(lines, "StressEncodingEffectRows", effect_rows)

    stress_ablation = [
        row for row in ablation_summary if row["dataset"] in {"Stress", "Stress-high"}
    ]
    stress_formula_rows: list[str] = []
    stress_runtime_rows: list[str] = []
    for dataset in ("Stress", "Stress-high"):
        for objective in ("BG-d2", "IR", "IR-IM-IS"):
            group = [
                row
                for row in stress_ablation
                if row["dataset"] == dataset and row["objective"] == objective
            ]
            for position, row in enumerate(group):
                dataset_cell = dataset if objective == "BG-d2" and position == 0 else ""
                objective_cell = OBJECTIVE_TEX[objective] if position == 0 else ""
                reduction = row["paired_median_clause_reduction_percent"]
                reduction_text = (
                    "--" if reduction == "" else _tex_number(reduction, decimals=2)
                )
                component_cells = _tex_component_cells(row["configuration"])
                stress_formula_rows.append(
                    f"{dataset_cell} & {objective_cell} & {component_cells} & "
                    f"{_tex_number(row['median_variables'])} & "
                    f"{_tex_number(row['median_clauses'])} & "
                    f"{_tex_number(row['median_literals'])} & {reduction_text}"
                    + r" \\"
                )
                stress_runtime_rows.append(
                    f"{dataset_cell} & {objective_cell} & {component_cells} & "
                    f"{row['solved']} & {row['timeout']} & "
                    f"{_tex_number(row['median_runtime_seconds'], decimals=3)} & "
                    f"{_tex_number(row['mean_PAR2_seconds'], decimals=3)} & "
                    f"{_tex_number(row['median_peak_RSS_mb'], decimals=2)}"
                    + r" \\"
                )
            if objective != "IR-IM-IS":
                stress_formula_rows.append(r"\addlinespace[1pt]")
                stress_runtime_rows.append(r"\addlinespace[1pt]")
        if dataset == "Stress":
            stress_formula_rows.append(r"\addlinespace")
            stress_runtime_rows.append(r"\addlinespace")
    _write_macro(lines, "StressAblationFormulaRows", stress_formula_rows)
    _write_macro(lines, "StressAblationRuntimeRows", stress_runtime_rows)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mapped_compact_objective_rows(
    rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Map completed optimized runs to the reader-facing target identifiers."""

    source_configs = {
        "compact_cdf_ir": "compact_objective_ir",
        "compact_cdf_ir_im_is": "compact_objective_ir_im_is",
    }
    mapped: list[dict[str, str]] = []
    for row in rows:
        target = source_configs.get(str(row.get("planned_configuration_id", "")))
        if target is None:
            continue
        copied = dict(row)
        copied["planned_configuration_id"] = target
        mapped.append(copied)
    return mapped


def ablation_table(
    compact_rows: Sequence[dict[str, str]],
    objective_aware_rows: Sequence[dict[str, str]],
    attainable_extension_rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    def index_group(
        rows: Sequence[dict[str, object] | dict[str, str]],
    ) -> dict[str, dict[str, object] | dict[str, str]]:
        indexed = {str(row["instance_content_id"]): row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError("ablation rows must be unique by instance_content_id")
        return indexed

    def paired_clause_reduction(
        baseline_rows: Sequence[dict[str, object] | dict[str, str]],
        candidate_rows: Sequence[dict[str, object] | dict[str, str]],
    ) -> float:
        baseline = index_group(baseline_rows)
        candidate = index_group(candidate_rows)
        if set(baseline) != set(candidate):
            raise ValueError("ablation comparison requires identical instance sets")
        reductions = [
            100.0
            * (
                int(baseline[key]["n_total_clauses"])
                - int(candidate[key]["n_total_clauses"])
            )
            / int(baseline[key]["n_total_clauses"])
            for key in sorted(baseline)
        ]
        return median(reductions)

    paths = {
        "BG-d2": (
            ("bg_reference", "Reference", "B", None),
            ("bg_a", "Zero-break simplification", "B + ZB", "bg_reference"),
            ("bg_c", "Demand-driven states", "B + DS", "bg_reference"),
            ("bg_ac", "Combined simplifications", "B + ZB + DS", "bg_reference"),
            ("bg_acf", "Adaptive at-most-one encoding", "B + ZB + DS + AMO", "bg_ac"),
        ),
        "IR": (
            ("ir_reference", "Reference", "B", None),
            ("ir_c", "Demand-driven states", "B + DS", "ir_reference"),
            ("ir_d", "Direct range costs", "B + RC", "ir_reference"),
            ("ir_cd", "Combined simplifications", "B + DS + RC", "ir_reference"),
            ("ir_cdf", "Adaptive at-most-one encoding", "B + DS + RC + AMO", "ir_cd"),
        ),
        "IR-IM-IS": (
            ("irimis_reference", "Reference", "B", None),
            ("irimis_c", "Demand-driven states", "B + DS", "irimis_reference"),
            ("irimis_d", "Direct range costs", "B + RC", "irimis_reference"),
            ("irimis_cd", "Combined simplifications", "B + DS + RC", "irimis_reference"),
            ("irimis_cdf", "Adaptive at-most-one encoding", "B + DS + RC + AMO", "irimis_cd"),
        ),
    }
    table: list[dict[str, object]] = []
    for objective, steps in paths.items():
        groups = {
            configuration: [
                row
                for row in compact_rows
                if row.get("planned_configuration_id") == configuration
            ]
            for configuration, _, _, _ in steps
        }
        for configuration, step, symbol, baseline_configuration in steps:
            group = groups[configuration]
            table.append(
                {
                    "objective": objective,
                    "step": step,
                    "configuration": symbol,
                    "baseline_configuration": (
                        "" if baseline_configuration is None else baseline_configuration
                    ),
                    "median_variables": median(int(row["n_vars"]) for row in group),
                    "median_clauses": median(int(row["n_total_clauses"]) for row in group),
                    "median_literals": median(int(row["n_total_literals"]) for row in group),
                    "paired_median_clause_reduction_percent": (
                        ""
                        if baseline_configuration is None
                        else paired_clause_reduction(
                            groups[baseline_configuration], group
                        )
                    ),
                }
            )
        if objective in {"IR", "IR-IM-IS"}:
            group = [
                row
                for row in attainable_extension_rows
                if row.get("objective") == objective
                and row.get("encoding") == "Attainable-Extension"
            ]
            table.append(
                {
                    "objective": objective,
                    "step": "Optional attainable-value compression",
                    "configuration": "B + DS + RC + AMO + AV",
                    "baseline_configuration": (
                        "ir_cdf" if objective == "IR" else "irimis_cdf"
                    ),
                    "median_variables": median(int(row["n_vars"]) for row in group),
                    "median_clauses": median(int(row["n_total_clauses"]) for row in group),
                    "median_literals": median(int(row["n_total_literals"]) for row in group),
                    "paired_median_clause_reduction_percent": paired_clause_reduction(
                        groups["ir_cdf" if objective == "IR" else "irimis_cdf"],
                        group,
                    ),
                }
            )

    extra_modes = (
        ("is", "IS", "Direct idle-slot clauses"),
        ("isq", "ISQ", "Attainable-value compression"),
        ("im_is", "IM-IS", "Attainable-value compression"),
        ("ir_is", "IR-IS", "Attainable-value compression"),
    )
    for mode, display, final_step in extra_modes:
        groups = {
            encoding: [
                row
                for row in objective_aware_rows
                if row.get("objective_mode") == mode
                and row.get("compact_encoding") == encoding
            ]
            for encoding in ("optimized", "objective_aware")
        }
        final_symbol = "DI" if mode == "is" else "AV"
        for encoding, step, symbol in (
            ("optimized", "Dense idle thresholds", "DT"),
            ("objective_aware", final_step, final_symbol),
        ):
            group = groups[encoding]
            table.append(
                {
                    "objective": display,
                    "step": step,
                    "configuration": symbol,
                    "baseline_configuration": "" if encoding == "optimized" else "DT",
                    "median_variables": median(int(row["n_vars"]) for row in group),
                    "median_clauses": median(int(row["n_total_clauses"]) for row in group),
                    "median_literals": median(int(row["n_total_literals"]) for row in group),
                    "paired_median_clause_reduction_percent": (
                        ""
                        if encoding == "optimized"
                        else paired_clause_reduction(groups["optimized"], group)
                    ),
                }
            )
    return table


def reuse_row_errors(row: dict[str, str]) -> list[str]:
    """Validate a reusable result while treating provenance as descriptive."""

    errors: list[str] = []
    status = str(row.get("status", "")).strip()
    if status not in TERMINAL_STATUSES:
        errors.append(f"nonterminal_status={status or 'missing'}")
    if str(row.get("campaign_executor_returncode", "")).strip() not in {"", "0"}:
        errors.append("nonzero_executor_returncode")
    for field in ("error_type", "error_message", "validation_errors"):
        if str(row.get(field, "")).strip():
            errors.append(f"nonempty_{field}")
    for field in ("instance_content_id", "instance_sha256"):
        if not str(row.get(field, "")).strip():
            errors.append(f"missing_{field}")
    for field in ("runtime_seconds", "peak_memory_mb"):
        try:
            value = float(str(row.get(field, "")).strip())
        except ValueError:
            errors.append(f"invalid_{field}")
            continue
        if not math.isfinite(value) or value < 0:
            errors.append(f"invalid_{field}")
    for field in FORMULA_FIELDS:
        try:
            value = int(float(str(row.get(field, "")).strip()))
        except ValueError:
            errors.append(f"invalid_{field}")
            continue
        if value <= 0:
            errors.append(f"invalid_{field}")
    if status == "OPTIMAL" and not normalized_vector(row):
        errors.append("missing_optimal_objective_vector")
    if status == "TIMEOUT" and str(row.get("runtime_censored", "")).lower() not in {
        "1",
        "true",
    }:
        errors.append("uncensored_timeout")
    return errors


def reuse_inventory(
    main_rows: Sequence[dict[str, str]],
    compact_rows: Sequence[dict[str, str]],
    conference_ir_rerun_rows: Sequence[dict[str, str]] = (),
) -> list[dict[str, object]]:
    selected_main = [
        row
        for row in main_rows
        if row.get("planned_configuration_id")
        in {
            "published_2022_bg_d2",
            "published_2022_ir",
            "published_2022_ir_im_is",
            "compact_acf_bg_d2",
            "compact_cdf_ir",
            "compact_cdf_ir_im_is",
        }
    ]
    selected_compact = [
        row
        for row in compact_rows
        if row.get("planned_configuration_id")
        in {"bg_reference", "ir_reference", "irimis_reference"}
    ]
    official_content_ids = {
        str(row.get("instance_content_id", "")) for row in selected_compact
    }
    selected_reruns = [
        row
        for row in conference_ir_rerun_rows
        if row.get("planned_configuration_id") == "conference_ir_rerun"
        and row.get("instance_content_id") in official_content_ids
    ]
    candidates = selected_main + selected_compact + selected_reruns
    inventory: list[dict[str, object]] = []
    for row in candidates:
        source_configuration = str(row.get("planned_configuration_id", ""))
        target_configuration = SOURCE_TO_TARGET_CONFIGURATION[source_configuration]
        source_repetition = int(row.get("repetition") or 0)
        target_repetition = (
            source_repetition + 1
            if source_configuration == "conference_ir_rerun"
            else source_repetition
        )
        objective, encoding = CONFIGURATION_MAP[target_configuration]
        errors = reuse_row_errors(row)
        inventory.append(
            {
                "source_campaign_id": row.get("campaign_id", ""),
                "source_configuration_id": source_configuration,
                "source_repetition": source_repetition,
                "target_configuration_id": target_configuration,
                "target_repetition": target_repetition,
                "objective": objective,
                "encoding": encoding,
                "instance": row.get("instance", ""),
                "instance_content_id": row.get("instance_content_id", ""),
                "instance_sha256": row.get("instance_sha256", ""),
                "status": row.get("status", ""),
                "runtime_seconds": row.get("runtime_seconds", ""),
                "accepted": not errors,
                "rejection_reasons": ";".join(errors),
                "git_commit": row.get("git_commit", ""),
                "python_version": row.get("python_version", ""),
                "solver_binary_sha256": row.get("solver_binary_sha256", ""),
                "source_run_key": row.get("run_key", ""),
            }
        )
    return sorted(
        inventory,
        key=lambda row: (
            str(row["target_configuration_id"]),
            int(row["target_repetition"]),
            str(row["instance_content_id"]),
        ),
    )


def reuse_audit(inventory: Sequence[dict[str, object]]) -> dict[str, object]:
    candidates = list(inventory)
    hashes = sorted({row.get("solver_binary_sha256", "") for row in candidates})
    commits = sorted({row.get("git_commit", "") for row in candidates})
    versions = sorted({row.get("python_version", "") for row in candidates})
    accepted = [row for row in candidates if bool(row.get("accepted"))]
    target_keys = [
        (
            str(row.get("target_configuration_id", "")),
            int(row.get("target_repetition") or 0),
            str(row.get("instance_content_id", "")),
        )
        for row in accepted
    ]
    duplicate_targets = sum(count != 1 for count in Counter(target_keys).values())
    approved = (
        len(candidates) == EXPECTED_REUSED_ROWS
        and len(accepted) == EXPECTED_REUSED_ROWS
        and len(set(target_keys)) == EXPECTED_REUSED_ROWS
        and duplicate_targets == 0
    )
    return {
        "candidate_job_count": len(candidates),
        "planned_candidate_job_count": EXPECTED_REUSED_ROWS,
        "accepted_candidate_job_count": len(accepted),
        "rejected_candidate_job_count": len(candidates) - len(accepted),
        "unique_target_job_count": len(set(target_keys)),
        "duplicate_target_job_count": duplicate_targets,
        "solver_binary_sha256_values": hashes,
        "git_commit_values": commits,
        "python_version_values": versions,
        "provenance_treated_as_descriptive": True,
        "environment_assumption": (
            "All accepted runs used the common c4-highcpu-8 GCP profile; "
            "commit, Python, and solver-build identifiers do not block reuse."
        ),
        "approved": approved,
        "reason": (
            "all 2,898 planned rows passed result validation under the common-GCP assumption"
            if approved
            else "one or more planned reuse rows are missing, duplicated, or invalid"
        ),
    }


def single_run_matrix_rows(
    main_rows: Sequence[dict[str, str]],
    compact_rows: Sequence[dict[str, str]],
    *,
    expected_instances: int = EXPECTED_INSTANCES,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Return one validated run for every encoding-objective-instance cell."""

    source_configurations = set(SOURCE_TO_TARGET_CONFIGURATION)
    selected = [
        row
        for row in (*main_rows, *compact_rows)
        if row.get("planned_configuration_id") in source_configurations
        and int(row.get("repetition") or 0) == 1
    ]
    mapped: list[dict[str, str]] = []
    invalid: list[str] = []
    for row in selected:
        errors = reuse_row_errors(row)
        if errors:
            invalid.append(f"{row.get('run_key', 'row')}: {';'.join(errors)}")
            continue
        copied = dict(row)
        copied["planned_configuration_id"] = SOURCE_TO_TARGET_CONFIGURATION[
            str(row["planned_configuration_id"])
        ]
        copied["repetition"] = "1"
        mapped.append(copied)
    if invalid:
        raise ValueError("invalid single-run rows: " + "; ".join(invalid[:5]))

    validation = validate_complete_matrix(
        mapped,
        expected_instances=expected_instances,
        expected_repetitions=1,
    )
    if not validation["valid"]:
        raise ValueError(
            "incomplete single-run matrix: " + "; ".join(validation["errors"])
        )
    agreement = objective_agreement_errors(mapped)
    if agreement:
        raise ValueError(
            "single-run objective agreement failed: " + "; ".join(agreement[:5])
        )
    return mapped, validation


def remaining_run_plan(
    manifest_rows: Sequence[dict[str, str]],
    inventory: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    reused = {
        (
            str(row["target_configuration_id"]),
            int(row["target_repetition"]),
            str(row["instance_content_id"]),
        )
        for row in inventory
        if bool(row.get("accepted"))
    }
    remaining: list[dict[str, object]] = []
    for configuration, (objective, encoding) in CONFIGURATION_MAP.items():
        for repetition in range(1, EXPECTED_REPETITIONS + 1):
            for instance in manifest_rows:
                key = (configuration, repetition, instance["content_id"])
                if key in reused:
                    continue
                remaining.append(
                    {
                        "target_configuration_id": configuration,
                        "objective": objective,
                        "encoding": encoding,
                        "repetition": repetition,
                        "instance": instance["canonical_instance"],
                        "instance_content_id": instance["content_id"],
                        "instance_sha256": instance["sha256"],
                    }
                )
    return remaining


def validate_matched_matrix(rows: Sequence[dict[str, str]]) -> dict[str, object]:
    """Check the full 6,102-row matrix before any publication output is changed."""
    validations: dict[str, object] = {}
    errors: list[str] = []
    if len(rows) != 6102:
        errors.append(f"row_count={len(rows)}, expected=6102")
    groups = {
        "All-126": [row for row in rows if str(row.get("instance_variant", "")) not in {"prec30", "prec35", "prec40", "prec50", "prec60"}],
        "Stress": [row for row in rows if row.get("instance_variant") in STRESS_VARIANTS["Stress"]],
        "Stress-high": [row for row in rows if row.get("instance_variant") in STRESS_VARIANTS["Stress-high"]],
    }
    for dataset, group in groups.items():
        validation = validate_complete_matrix(group, expected_instances=BENCHMARK_SIZES[dataset])
        validations[dataset] = validation
        errors.extend(f"{dataset}: {error}" for error in validation["errors"])
        errors.extend(formula_invariance_errors(group))
        errors.extend(objective_agreement_errors(group))
    for row in rows:
        errors.extend(f"{row.get('run_key')}: {error}" for error in reuse_row_errors(row))
    execution_groups: dict[tuple[str, str], set[tuple[str, ...]]] = defaultdict(set)
    feasibility: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row.get("planned_configuration_id") not in CONFIGURATION_MAP:
            continue
        key = (row["planned_configuration_id"], row["instance_sha256"])
        identity = tuple(str(row.get(field, "")) for field in ("run_started_utc", "hostname", "solver_binary_sha256"))
        if not all(identity) or identity in execution_groups[key]:
            errors.append(f"duplicate or missing execution provenance: {key}")
        execution_groups[key].add(identity)
        mode = row.get("objective_mode") or CONFIGURATION_MAP[row["planned_configuration_id"]][0]
        feasibility[(mode, row["instance_sha256"])].add(row["status"])
    if any({"OPTIMAL", "UNSAT"} <= statuses for statuses in feasibility.values()):
        errors.append("contradictory feasibility certificates")
    if errors:
        raise ValueError("matched matrix is incomplete or invalid: " + "; ".join(errors[:12]))
    return {"valid": True, "row_count": len(rows), "datasets": validations}


def analyze_matched_campaign(
    path: Path, *, output_dir: Path, latex_output: Path
) -> dict[str, object]:
    """Publish tables only after all three sets have three validated runs."""
    from Matched_Encoding_Evaluation import STATIC_FIELDS, recover_assignment
    from B2B_Instance import validate_schedule_assignment
    from Journal_Metrics import evaluate_journal_schedule
    from Conference_Consistency import conference_consistency_report

    rows = read_rows([path])
    validation = validate_matched_matrix(rows)
    conference_validation = conference_consistency_report(rows)
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    instances: dict[str, object] = {}
    for row in rows:
        grouped[(row["planned_configuration_id"], row["instance_sha256"])].append(row)
        if row["status"] != "OPTIMAL":
            continue
        sha = row["instance_sha256"]
        if sha not in instances:
            instance_path = PROJECT_ROOT / row["instance_path"]
            from Dataset_Manifest import file_sha256
            if file_sha256(instance_path) != sha:
                raise ValueError("analysis benchmark SHA-256 mismatch")
            instances[sha] = read_instance(instance_path)
        instance = instances[sha]
        assignment = recover_assignment(row, instance.n_meetings)
        errors = validate_schedule_assignment(instance, assignment)
        metrics = evaluate_journal_schedule(instance, assignment, objective_mode=row["objective_mode"])
        if metrics.objective_vector != tuple(int(value) for value in normalized_vector(row).split(",")):
            errors.append("independent objective mismatch")
        if row["objective_mode"] == "bg_d2" and not metrics.historical_fairness_cap_satisfied:
            errors.append("homogeneity violation")
        if errors:
            raise ValueError(f"{row['run_key']}: {errors}")
    outcome_rows: list[dict[str, object]] = []
    canonical: list[dict[str, str]] = []
    quality_rows: list[dict[str, str]] = []
    for (configuration, sha), group in sorted(grouped.items()):
        group = sorted(group, key=lambda row: int(row["repetition"]))
        signatures = {tuple(str(row.get(field, "")) for field in STATIC_FIELDS) for row in group}
        if len(signatures) != 1:
            raise ValueError(f"static formula differs across repetitions: {configuration}/{sha}")
        canonical.append(group[0])
        quality_rows.append(canonical_schedule_row(group) if any(row["status"] == "OPTIMAL" for row in group) else group[0])
        summary = aggregate_instance_repetitions(group, time_rss_policy="optimal-runs")
        dataset = "All-126" if group[0]["instance_variant"] not in {"prec30", "prec35", "prec40", "prec50", "prec60"} else stress_dataset(group[0])
        item: dict[str, object] = {"dataset": dataset, "configuration": configuration, "instance_sha256": sha}
        for index, row in enumerate(group, 1):
            item.update({f"status_{index}": row["status"], f"time_{index}_seconds": row["runtime_seconds"],
                         f"PAR2_{index}_seconds": 2 * TIMEOUT_SECONDS if row["status"] == "TIMEOUT" else numeric(row, "runtime_seconds")})
        item.update(summary)
        outcome_rows.append(item)
    official = [row for row in rows if row["instance_variant"] not in {"prec30", "prec35", "prec40", "prec50", "prec60"}]
    stress = [row for row in rows if row["instance_variant"] in {"prec30", "prec35", "prec40", "prec50", "prec60"}]
    canonical_official = [row for row in canonical if row["instance_variant"] not in {"prec30", "prec35", "prec40", "prec50", "prec60"}]
    canonical_stress = [row for row in canonical if row["instance_variant"] in {"prec30", "prec35", "prec40", "prec50", "prec60"}]
    formula_rows = formula_rows_from_mapped_runs(canonical_official)
    formula_summary, formula_reductions, formula_effects = benchmark_formula_tables(formula_rows, canonical_stress)
    runtime_summary, runtime_ratios, runtime_effects, timeouts = benchmark_runtime_tables(
        official, stress, expected_repetitions=3, time_rss_policy="optimal-runs")
    compact_rows = read_rows([PROJECT_ROOT / "outputs/journal/compact-objectives-merged/normalized/detailed.csv"])
    stress_ablation = read_rows(sorted((PROJECT_ROOT / "outputs/journal/compact-ablation-stress-sessions").glob("a*/normalized/detailed.csv")))
    content_ids = {row["instance_sha256"]: row["instance_content_id"] for row in canonical}
    for row in (*compact_rows, *stress_ablation):
        row["instance_content_id"] = content_ids[row["instance_sha256"]]
    ablation = primary_ablation_summary(canonical_primary_ablation_rows(compact_rows, canonical_official), datasets=("All-126",))
    ablation += primary_ablation_summary(canonical_primary_ablation_rows(stress_ablation, canonical_stress), datasets=("Stress", "Stress-high"))
    stress_quality, stress_membership = stress_quality_tables([row for row in quality_rows if row["instance_variant"] in {"prec30", "prec35", "prec40", "prec50", "prec60"}])
    official_quality = [row for row in quality_rows if row["instance_variant"] not in {"prec30", "prec35", "prec40", "prec50", "prec60"}]
    quality_summary, quality_pairs, membership, quality_effects = quality_tables(official_quality, read_rows([PROJECT_ROOT / "outputs/journal/bg-dinf-quality/normalized/detailed.csv"]))
    # All checks above precede publication writes, including the completion gate.
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, values in {
        "benchmark_formula_summary.csv": formula_summary,
        "benchmark_formula_reductions.csv": formula_reductions,
        "benchmark_formula_effects.csv": formula_effects,
        "formula_effects.csv": [row for row in formula_effects if row["dataset"] == "All-126"],
        "benchmark_runtime_summary.csv": runtime_summary,
        "benchmark_runtime_ratios.csv": runtime_ratios,
        "benchmark_runtime_effects.csv": runtime_effects,
        "runtime_effects.csv": [row for row in runtime_effects if row["dataset"] == "All-126"],
        "benchmark_timeouts.csv": timeouts,
        "repetition_outcomes.csv": outcome_rows,
        "benchmark_primary_ablation_summary.csv": ablation,
        "stress_quality_pairs.csv": stress_quality,
        "stress_quality_membership.csv": stress_membership,
        "quality_summary.csv": quality_summary, "quality_pairs.csv": quality_pairs,
        "quality_effects.csv": quality_effects,
        "analysis_set_membership.csv": membership,
    }.items():
        write_csv(output_dir / name, values)
    write_latex_result_rows(latex_output, formula_summary=formula_summary,
        formula_reductions=formula_reductions, runtime_summary=runtime_summary,
        runtime_ratios=runtime_ratios, runtime_effects=runtime_effects,
        stress_quality=stress_quality, ablation_summary=ablation,
        quality_summary=quality_summary, quality_pairs=quality_pairs,
        aggregation_description="three repetitions; time/RSS use OPTIMAL runs for solved-feasible instances; PAR-2 retains all timeouts; ablation runtime remains single-run")
    (output_dir / "conference_consistency_report.json").write_text(
        json.dumps(conference_validation, indent=2) + "\n")
    report = {
        "production_campaign_complete": True, "aggregation": "three_repetitions",
        "matrix_validation": validation, "single_run_results_available": True,
        "source_results": str(path.resolve()),
        "source_results_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "conference_consistency": "passed: per-instance IR outcomes and optima; Compact-Base formula identity",
        "time_rss_policy": "optimal-runs",
        "time_rss_rule": "median of available OPTIMAL runs per OPTIMAL instance; otherwise all three runs; then median over instances",
        "outcome_rule": "OPT if at least one repetition proves optimality; UNSAT if proven; otherwise TIMEOUT",
        "paired_runtime_rule": "all three repetitions OPTIMAL in both encodings",
        "PAR2_rule": "penalize each run before taking the per-instance median, then average instances",
        "ablation_runtime": "descriptive single-run measurements",
        "manuscript_prose_review_required": True,
    }
    report["source_data_sha256"] = {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                                    for item in sorted(output_dir.glob("*.csv"))}
    (output_dir / "analysis_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matched-results", type=Path, help="complete 6,102-row matched campaign; publish three-repetition statistics")
    parser.add_argument(
        "--campaign-results",
        type=Path,
        default=PROJECT_ROOT / "outputs/journal/cross-generation-evaluation/normalized/detailed.csv",
    )
    parser.add_argument(
        "--main-results",
        type=Path,
        default=PROJECT_ROOT / "outputs/journal/main-objectives-merged/normalized/detailed.csv",
    )
    parser.add_argument(
        "--compact-results",
        type=Path,
        action="append",
        default=None,
    )
    parser.add_argument(
        "--conference-ir-reruns",
        type=Path,
        default=(
            PROJECT_ROOT
            / "outputs/journal/conference-ir-reruns/normalized/detailed.csv"
        ),
    )
    parser.add_argument(
        "--objective-aware-formulas",
        type=Path,
        default=PROJECT_ROOT / "outputs/objective_aware_formula_audit.csv",
    )
    parser.add_argument(
        "--bg-quality-results",
        type=Path,
        default=PROJECT_ROOT / "outputs/journal/bg-dinf-quality/normalized/detailed.csv",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "instances_manifest.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "outputs/cross_generation_analysis",
    )
    parser.add_argument(
        "--stress-main-root",
        type=Path,
        default=PROJECT_ROOT / "outputs/journal/main-stress-sessions",
    )
    parser.add_argument(
        "--stress-ablation-root",
        type=Path,
        default=PROJECT_ROOT / "outputs/journal/compact-ablation-stress-sessions",
    )
    parser.add_argument(
        "--latex-output",
        type=Path,
        default=(
            PROJECT_ROOT.parent
            / "JAIR_Example_Template"
            / "generated-results-rows.tex"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.matched_results is not None:
        report = analyze_matched_campaign(args.matched_results, output_dir=args.output_dir, latex_output=args.latex_output)
        print(json.dumps(report, indent=2))
        return 0
    compact_paths = args.compact_results or [
        PROJECT_ROOT / "outputs/journal/compact-objectives/shard-0-of-2/normalized/detailed.csv",
        PROJECT_ROOT / "outputs/journal/compact-objectives/shard-1-of-2/normalized/detailed.csv",
    ]
    main_rows = read_rows([args.main_results])
    compact_rows = read_rows(compact_paths)
    conference_ir_rerun_rows = read_rows([args.conference_ir_reruns])
    objective_aware_rows = read_rows([args.objective_aware_formulas])
    manifest_rows = read_rows([args.manifest])
    stress_main_paths = sorted(args.stress_main_root.glob("m*/normalized/detailed.csv"))
    stress_ablation_paths = sorted(
        args.stress_ablation_root.glob("a*/normalized/detailed.csv")
    )
    if len(stress_main_paths) != 6 or len(stress_ablation_paths) != 15:
        raise ValueError(
            "stress analysis requires six matched sessions and 15 ablation sessions"
        )
    stress_main_rows = read_rows(stress_main_paths)
    stress_ablation_rows = read_rows(stress_ablation_paths)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    formula_rows = build_formula_audit(main_rows, manifest=args.manifest)
    attainable_extension_rows = build_attainable_extension_audit(
        manifest=args.manifest
    )
    formula_summary, formula_reductions, formula_ratios = formula_tables(formula_rows)
    write_csv(args.output_dir / "formula_rows.csv", formula_rows)
    write_csv(args.output_dir / "formula_summary.csv", formula_summary)
    write_csv(args.output_dir / "formula_reductions.csv", formula_reductions)
    write_csv(args.output_dir / "formula_ratios.csv", formula_ratios)
    write_csv(
        args.output_dir / "formula_effects.csv",
        formula_effect_rows(formula_rows),
    )
    write_csv(
        args.output_dir / "attainable_extension_formula_rows.csv",
        attainable_extension_rows,
    )
    write_csv(
        args.output_dir / "ablation_summary.csv",
        ablation_table(
            compact_rows,
            objective_aware_rows,
            attainable_extension_rows,
        ),
    )
    inventory = reuse_inventory(
        main_rows, compact_rows, conference_ir_rerun_rows
    )
    remaining = remaining_run_plan(manifest_rows, inventory)
    write_csv(args.output_dir / "reuse_inventory.csv", inventory)
    write_csv(args.output_dir / "remaining_runs.csv", remaining)

    single_run_rows, single_run_validation = single_run_matrix_rows(
        main_rows, compact_rows
    )
    stress_single_run_rows, stress_single_run_validation = single_run_matrix_rows(
        stress_main_rows,
        stress_ablation_rows,
        expected_instances=EXPECTED_STRESS_INSTANCES,
    )

    benchmark_formula_summary, benchmark_formula_reductions, benchmark_formula_effects = (
        benchmark_formula_tables(formula_rows, stress_single_run_rows)
    )
    write_csv(
        args.output_dir / "benchmark_formula_summary.csv",
        benchmark_formula_summary,
    )
    write_csv(
        args.output_dir / "benchmark_formula_reductions.csv",
        benchmark_formula_reductions,
    )
    write_csv(
        args.output_dir / "benchmark_formula_effects.csv",
        benchmark_formula_effects,
    )

    single_runtime, single_ratios, single_timeouts = runtime_tables(single_run_rows)
    write_csv(args.output_dir / "runtime_summary_single_run.csv", single_runtime)
    write_csv(args.output_dir / "runtime_ratios_single_run.csv", single_ratios)
    write_csv(args.output_dir / "timeouts_single_run.csv", single_timeouts)
    write_csv(
        args.output_dir / "runtime_effects_single_run.csv",
        runtime_effect_rows(single_run_rows),
    )
    (
        benchmark_runtime_summary,
        benchmark_runtime_ratios,
        benchmark_runtime_effects,
        benchmark_timeouts,
    ) = benchmark_runtime_tables(single_run_rows, stress_single_run_rows)
    write_csv(
        args.output_dir / "benchmark_runtime_summary.csv",
        benchmark_runtime_summary,
    )
    write_csv(
        args.output_dir / "benchmark_runtime_ratios.csv",
        benchmark_runtime_ratios,
    )
    write_csv(
        args.output_dir / "benchmark_runtime_effects.csv",
        benchmark_runtime_effects,
    )
    write_csv(args.output_dir / "benchmark_timeouts.csv", benchmark_timeouts)

    bg_rows = read_rows([args.bg_quality_results])
    quality_summary, quality_pairs, membership, quality_effects = quality_tables(
        mapped_compact_objective_rows(main_rows), bg_rows
    )
    write_csv(args.output_dir / "quality_summary.csv", quality_summary)
    write_csv(args.output_dir / "quality_pairs.csv", quality_pairs)
    write_csv(args.output_dir / "quality_effects.csv", quality_effects)
    write_csv(args.output_dir / "analysis_set_membership.csv", membership)

    stress_quality, stress_quality_membership = stress_quality_tables(
        stress_single_run_rows
    )
    write_csv(args.output_dir / "stress_quality_pairs.csv", stress_quality)
    write_csv(
        args.output_dir / "stress_quality_membership.csv",
        stress_quality_membership,
    )

    canonical_official_ablation = canonical_primary_ablation_rows(
        compact_rows,
        single_run_rows,
    )
    canonical_stress_ablation = canonical_primary_ablation_rows(
        stress_ablation_rows,
        stress_single_run_rows,
    )
    primary_ablation = primary_ablation_summary(
        canonical_official_ablation,
        datasets=("All-126",),
    ) + primary_ablation_summary(
        canonical_stress_ablation,
        datasets=("Stress", "Stress-high"),
    )
    write_csv(
        args.output_dir / "benchmark_primary_ablation_summary.csv",
        primary_ablation,
    )
    write_latex_result_rows(
        args.latex_output,
        formula_summary=benchmark_formula_summary,
        formula_reductions=benchmark_formula_reductions,
        runtime_summary=benchmark_runtime_summary,
        runtime_ratios=benchmark_runtime_ratios,
        runtime_effects=benchmark_runtime_effects,
        stress_quality=stress_quality,
        ablation_summary=primary_ablation,
        quality_summary=quality_summary, quality_pairs=quality_pairs,
    )

    stress_endpoint_formula_errors = endpoint_formula_equivalence_errors(
        stress_ablation_rows,
        stress_single_run_rows,
    )
    if stress_endpoint_formula_errors:
        raise ValueError(
            "stress endpoint formula audit failed: "
            + "; ".join(stress_endpoint_formula_errors[:5])
        )

    report: dict[str, object] = {
        "formula_audit_rows": len(formula_rows),
        "reuse_audit": reuse_audit(inventory),
        "remaining_run_count": len(remaining),
        "quality_analysis_instances": int(quality_summary[0]["instances"]),
        "single_run_matrix_validation": single_run_validation,
        "stress_single_run_matrix_validation": stress_single_run_validation,
        "stress_matched_row_count": len(stress_single_run_rows),
        "stress_dataset_counts": {
            dataset: sum(
                stress_dataset(row) == dataset for row in stress_single_run_rows
            )
            for dataset in ("Stress", "Stress-high")
        },
        "stress_objective_agreement_errors": objective_agreement_errors(
            stress_single_run_rows
        ),
        "stress_endpoint_formula_equivalence_errors": stress_endpoint_formula_errors,
        "stress_quality_analysis_instances": {
            row["dataset"]: row["instances"]
            for row in stress_quality
            if row["measure"] == "idle_range_pstar"
        },
        "single_run_results_available": True,
        "production_campaign_complete": False,
    }
    if args.campaign_results.is_file():
        campaign_rows = read_rows([args.campaign_results])
        matrix = validate_complete_matrix(campaign_rows)
        report["matrix_validation"] = matrix
        report["formula_invariance_errors"] = formula_invariance_errors(campaign_rows)
        report["objective_agreement_errors"] = objective_agreement_errors(campaign_rows)
        if not matrix["valid"]:
            raise ValueError("incomplete cross-generation campaign: " + "; ".join(matrix["errors"]))
        if report["formula_invariance_errors"] or report["objective_agreement_errors"]:
            raise ValueError("cross-generation validation failed; inspect analysis_report.json")
        runtime_summary, runtime_ratios, timeouts = runtime_tables(campaign_rows)
        write_csv(args.output_dir / "runtime_summary.csv", runtime_summary)
        write_csv(args.output_dir / "runtime_ratios.csv", runtime_ratios)
        write_csv(args.output_dir / "timeouts.csv", timeouts)
        write_csv(
            args.output_dir / "runtime_effects.csv",
            runtime_effect_rows(campaign_rows),
        )

        quality_summary, quality_pairs, membership, quality_effects = quality_tables(campaign_rows, bg_rows)
        write_csv(args.output_dir / "quality_summary.csv", quality_summary)
        write_csv(args.output_dir / "quality_pairs.csv", quality_pairs)
        write_csv(args.output_dir / "quality_effects.csv", quality_effects)
        write_csv(args.output_dir / "analysis_set_membership.csv", membership)
        report["production_campaign_complete"] = True

    (args.output_dir / "analysis_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"formula_rows={len(formula_rows)} "
        f"campaign_complete={report['production_campaign_complete']} "
        f"output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
