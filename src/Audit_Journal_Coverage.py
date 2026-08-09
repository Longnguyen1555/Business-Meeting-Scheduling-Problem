from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any


FEATURES = (
    "n_meetings",
    "capacity_pressure",
    "participant_degree_gini",
    "domain_removal_ratio",
    "forbidden_ratio",
    "precedence_closure_density",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _summary(rows: list[dict[str, str]], feature: str) -> dict[str, float]:
    values = [float(row[feature]) for row in rows]
    return {
        "min": min(values),
        "q25": _quantile(values, 0.25),
        "median": _quantile(values, 0.5),
        "q75": _quantile(values, 0.75),
        "max": max(values),
    }


def _bin(value: float, cuts: tuple[float, float]) -> int:
    if value <= cuts[0]:
        return 0
    if value <= cuts[1]:
        return 1
    return 2


def audit_coverage(
    feature_rows: list[dict[str, str]],
    generation_rows: list[dict[str, str]],
    *,
    expected_development: int,
    expected_heldout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[str] = []
    generation_by_content = {
        row["content_id"]: row for row in generation_rows
    }
    generated = [
        row for row in feature_rows if row["dataset_id"] == "generated"
    ]
    reference = [
        row for row in feature_rows if row["dataset_id"] != "generated"
    ]
    for row in generated:
        metadata = generation_by_content.get(row["instance_content_id"])
        if metadata is None:
            errors.append(
                "generated feature has no generation row: "
                f"{row['instance_content_id']}"
            )
            row["_split"] = "unknown"
        else:
            row["_split"] = metadata["split"]
    development = [row for row in generated if row.get("_split") == "development"]
    heldout = [row for row in generated if row.get("_split") == "heldout"]
    if len(development) != expected_development:
        errors.append(
            f"development feature count {len(development)}!={expected_development}"
        )
    if len(heldout) != expected_heldout:
        errors.append(f"heldout feature count {len(heldout)}!={expected_heldout}")
    if len(generation_by_content) != len(generation_rows):
        errors.append("duplicate generated content_id")
    if not reference:
        errors.append("coverage audit has no non-generated reference rows")

    populations = {
        "reference": reference,
        "generated_all": generated,
        "generated_development": development,
        "generated_heldout": heldout,
    }
    summaries = {
        population: {
            feature: _summary(rows, feature)
            for feature in FEATURES
            if rows
        }
        for population, rows in populations.items()
    }
    range_overlap = {}
    if reference:
        for feature in FEATURES:
            reference_values = [float(row[feature]) for row in reference]
            lower, upper = min(reference_values), max(reference_values)
            range_overlap[feature] = {
                population: round(
                    sum(lower <= float(row[feature]) <= upper for row in rows)
                    / len(rows),
                    6,
                )
                for population, rows in (
                    ("generated_development", development),
                    ("generated_heldout", heldout),
                )
                if rows
            }
        if expected_development >= 81:
            for feature, overlaps in range_overlap.items():
                for population, fraction in overlaps.items():
                    if fraction < 0.10:
                        errors.append(
                            f"{population}/{feature} has <10% reference-range overlap"
                        )
            expanded_features = sum(
                range_overlap[feature].get("generated_development", 1.0) < 1.0
                for feature in FEATURES
            )
            if expanded_features < 3:
                errors.append(
                    "generated development extends fewer than three audited "
                    "feature ranges"
                )

    pair_rows: list[dict[str, Any]] = []
    if reference:
        cuts = {
            feature: (
                _quantile([float(row[feature]) for row in reference], 1 / 3),
                _quantile([float(row[feature]) for row in reference], 2 / 3),
            )
            for feature in FEATURES
        }
        for left, right in combinations(FEATURES, 2):
            occupied = {}
            for population, rows in populations.items():
                occupied[population] = {
                    (
                        _bin(float(row[left]), cuts[left]),
                        _bin(float(row[right]), cuts[right]),
                    )
                    for row in rows
                }
            for population in (
                "generated_all",
                "generated_development",
                "generated_heldout",
            ):
                pair_rows.append(
                    {
                        "feature_x": left,
                        "feature_y": right,
                        "population": population,
                        "occupied_cells_of_9": len(occupied[population]),
                        "reference_occupied_cells_of_9": len(
                            occupied["reference"]
                        ),
                        "shared_reference_cells": len(
                            occupied[population].intersection(
                                occupied["reference"]
                            )
                        ),
                    }
                )

    stratum_counter = Counter(
        (row["split"], row["structural_stratum_id"])
        for row in generation_rows
    )
    stratum_rows = [
        {"split": split, "structural_stratum_id": stratum, "count": count}
        for (split, stratum), count in sorted(stratum_counter.items())
    ]
    generated_strata = {
        row["structural_stratum_id"] for row in generation_rows
    }
    if expected_development >= 81 and len(generated_strata) != 81:
        errors.append(
            f"quota design covers {len(generated_strata)} strata instead of 81"
        )
    report = {
        "valid": not errors,
        "errors": errors,
        "feature_rows": len(feature_rows),
        "reference_rows": len(reference),
        "reference_lineages": len(
            {row["base_lineage_id"] for row in reference}
        ),
        "generated_rows": len(generated),
        "generated_lineages": len(
            {row["base_lineage_id"] for row in generated}
        ),
        "development_rows": len(development),
        "heldout_rows": len(heldout),
        "generated_strata": len(generated_strata),
        "features": list(FEATURES),
        "summaries": summaries,
        "generated_within_reference_range_fraction": range_overlap,
    }
    return report, pair_rows, stratum_rows


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Generated-300 marginal and joint feature coverage."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--generation-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-development", type=int, default=240)
    parser.add_argument("--expected-heldout", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    output = Path(args.output_dir)
    targets = (
        output / "coverage_summary.json",
        output / "pairwise_coverage.csv",
        output / "stratum_counts.csv",
    )
    existing = [path for path in targets if path.exists()]
    if existing and not args.overwrite:
        print(f"ERROR: coverage outputs already exist: {existing}")
        return 2
    output.mkdir(parents=True, exist_ok=True)
    report, pair_rows, stratum_rows = audit_coverage(
        _read_csv(Path(args.features)),
        _read_csv(Path(args.generation_manifest)),
        expected_development=args.expected_development,
        expected_heldout=args.expected_heldout,
    )
    targets[0].write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(
        targets[1],
        pair_rows,
        [
            "feature_x",
            "feature_y",
            "population",
            "occupied_cells_of_9",
            "reference_occupied_cells_of_9",
            "shared_reference_cells",
        ],
    )
    _write_csv(
        targets[2],
        stratum_rows,
        ["split", "structural_stratum_id", "count"],
    )
    print(
        f"coverage valid={report['valid']} generated={report['generated_rows']} "
        f"strata={report['generated_strata']}"
    )
    for error in report["errors"]:
        print(f"ERROR: {error}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
