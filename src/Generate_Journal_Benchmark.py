from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from B2B_Instance import B2BInstance, read_instance, validate_schedule_assignment
from Dataset_Manifest import MANIFEST_FIELDS, file_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DEFAULT_MASTER_SEED = 20260808

GENERATION_FIELDS = (
    "instance",
    "content_id",
    "sha256",
    "split",
    "generator_seed",
    "base_generator_seed",
    "base_lineage_id",
    "structural_stratum_id",
    "requested_n_meetings",
    "n_meetings",
    "n_business",
    "n_tables",
    "n_total_slots",
    "capacity_pressure_requested",
    "capacity_pressure_realized",
    "fixed_ratio_requested",
    "fixed_ratio_realized",
    "forbidden_ratio_requested",
    "forbidden_ratio_realized",
    "precedence_density_requested",
    "precedence_density_realized",
    "precedence_depth_requested",
    "precedence_candidate_edges",
    "precedence_direct_edges",
    "participant_degree_skew_requested",
    "participant_degree_gini_realized",
    "session_restricted_ratio_requested",
    "session_restricted_ratio_realized",
    "witness_sha256",
)

WITNESS_FIELDS = (
    "instance",
    "split",
    "base_lineage_id",
    "witness_sha256",
    "assignment_1_based",
)


@dataclass(frozen=True)
class DesignPoint:
    split: str
    index: int
    seed: int
    stratum_id: str
    n_meetings: int
    n_business: int
    n_total_slots: int
    capacity_pressure: float
    fixed_ratio: float
    forbidden_ratio: float
    precedence_density: float
    precedence_depth: float
    degree_skew: float
    session_restricted_ratio: float


def _write_csv(
    path: Path,
    fieldnames: Iterable[str],
    rows: Iterable[dict[str, Any]],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _assignment_sha256(assignment: list[int]) -> str:
    payload = ",".join(str(slot + 1) for slot in assignment)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _seed(master_seed: int, split: str, index: int, attempt: int = 0) -> int:
    payload = f"{master_seed}:{split}:{index}:{attempt}".encode("ascii")
    return int(hashlib.sha256(payload).hexdigest()[:16], 16)


def _level(value: int) -> str:
    return ("low", "mid", "high")[value]


def _stratified_value(rng: random.Random, level: int, low: float, high: float) -> float:
    width = (high - low) / 3
    return low + (level + rng.random()) * width


def _latin_permutations(count: int, rng: random.Random) -> dict[str, list[int]]:
    result = {}
    for name in ("meetings", "business", "slots", "depth", "session"):
        values = list(range(count))
        rng.shuffle(values)
        result[name] = values
    return result


def _lhs_min_distance(permutations: dict[str, list[int]], count: int) -> float:
    if count < 2:
        return math.inf
    names = tuple(permutations)
    best = math.inf
    for left in range(count):
        for right in range(left):
            squared = sum(
                (
                    (permutations[name][left] - permutations[name][right])
                    / count
                )
                ** 2
                for name in names
            )
            best = min(best, squared)
    return best


def _maximin_latin_permutations(
    count: int,
    rng: random.Random,
    *,
    candidates: int = 32,
) -> dict[str, list[int]]:
    """Select the best of deterministic randomized LHS candidate designs."""

    designs = [_latin_permutations(count, rng) for _ in range(candidates)]
    return max(designs, key=lambda value: _lhs_min_distance(value, count))


def _lhs_value(index: int, permutation: list[int], count: int) -> float:
    # Midpoint jitter is deterministic and avoids exact interval boundaries.
    return (permutation[index] + 0.5) / count


def build_design(
    *,
    split: str,
    count: int,
    master_seed: int,
) -> list[DesignPoint]:
    rng = random.Random(_seed(master_seed, split, count))
    permutations = _maximin_latin_permutations(count, rng)
    points: list[DesignPoint] = []
    for index in range(count):
        pressure_level = index % 3
        precedence_level = (index // 3) % 3
        forbidden_level = (index // 9) % 3
        skew_level = (index // 27) % 3
        stratum_id = "-".join(
            (
                f"cp-{_level(pressure_level)}",
                f"prec-{_level(precedence_level)}",
                f"forb-{_level(forbidden_level)}",
                f"skew-{_level(skew_level)}",
            )
        )
        accepted: DesignPoint | None = None
        for attempt in range(1000):
            point_rng = random.Random(_seed(master_seed, split, index, attempt))
            meetings_u = _lhs_value(index, permutations["meetings"], count)
            business_u = _lhs_value(index, permutations["business"], count)
            slots_u = _lhs_value(index, permutations["slots"], count)
            if attempt:
                # Constraint-aware retries retain the quota stratum but redraw
                # continuous size coordinates. Requested and realized values
                # remain recorded for the coverage audit.
                meetings_u = point_rng.random()
                business_u = point_rng.random()
                slots_u = point_rng.random()
            n_meetings = round(100 + meetings_u * 400)
            n_business = round(40 + business_u * 80)
            n_total_slots = round(8 + slots_u * 22)
            pressure = _stratified_value(point_rng, pressure_level, 0.55, 0.95)
            # Participant non-overlap imposes 2M <= P*T. Tables needed for the
            # requested pressure must also not exceed floor(P/2).
            max_meetings = math.floor(
                pressure * (n_business // 2) * n_total_slots
            )
            if max_meetings < 100 or n_meetings > max_meetings:
                continue
            fixed = point_rng.uniform(0.0, 0.15)
            forbidden = _stratified_value(
                point_rng,
                forbidden_level,
                0.0,
                0.35,
            )
            precedence = _stratified_value(
                point_rng,
                precedence_level,
                0.0,
                0.60,
            )
            depth = _lhs_value(index, permutations["depth"], count)
            session = 0.5 * _lhs_value(index, permutations["session"], count)
            skew = _stratified_value(point_rng, skew_level, 0.0, 1.0)
            accepted = DesignPoint(
                split=split,
                index=index,
                seed=_seed(master_seed, split, index),
                stratum_id=stratum_id,
                n_meetings=n_meetings,
                n_business=n_business,
                n_total_slots=n_total_slots,
                capacity_pressure=pressure,
                fixed_ratio=fixed,
                forbidden_ratio=forbidden,
                precedence_density=precedence,
                precedence_depth=depth,
                degree_skew=skew,
                session_restricted_ratio=session,
            )
            break
        if accepted is None:
            raise RuntimeError(f"cannot realize feasible design point {split}/{index}")
        points.append(accepted)
    return points


def _weighted_choice(
    rng: random.Random,
    candidates: list[int],
    weights: list[float],
) -> int:
    return rng.choices(candidates, weights=weights, k=1)[0]


def _construct_witness(
    point: DesignPoint,
) -> tuple[B2BInstance, list[int], dict[str, Any]]:
    rng = random.Random(point.seed)
    n_tables = math.ceil(
        point.n_meetings
        / (point.capacity_pressure * point.n_total_slots)
    )
    if n_tables > point.n_business // 2:
        raise AssertionError("design feasibility guard failed")

    assignment = [meeting % point.n_total_slots for meeting in range(point.n_meetings)]
    rng.shuffle(assignment)
    meetings_by_slot = [[] for _ in range(point.n_total_slots)]
    for meeting, slot in enumerate(assignment):
        meetings_by_slot[slot].append(meeting)
    if max(map(len, meetings_by_slot), default=0) > n_tables:
        raise AssertionError("balanced witness exceeds table capacity")

    # Exponential weights interpolate between nearly uniform and skewed
    # participant degrees. Selection remains without replacement per slot.
    ranks = list(range(point.n_business))
    participant_weights = [
        math.exp(-3.0 * point.degree_skew * rank / max(1, point.n_business - 1))
        for rank in ranks
    ]
    requested: list[tuple[int, int, int] | None] = [None] * point.n_meetings
    used_pairs: set[tuple[int, int]] = set()
    for slot, meetings in enumerate(meetings_by_slot):
        available = set(range(point.n_business))
        for meeting in meetings:
            pair: tuple[int, int] | None = None
            for _ in range(500):
                candidates = sorted(available)
                left = _weighted_choice(
                    rng,
                    candidates,
                    [participant_weights[value] for value in candidates],
                )
                right_candidates = [value for value in candidates if value != left]
                right = _weighted_choice(
                    rng,
                    right_candidates,
                    [participant_weights[value] for value in right_candidates],
                )
                candidate_pair = tuple(sorted((left, right)))
                if candidate_pair not in used_pairs:
                    pair = candidate_pair
                    break
            if pair is None:
                raise RuntimeError("cannot construct a unique participant pair")
            available.difference_update(pair)
            used_pairs.add(pair)
            if rng.random() < point.session_restricted_ratio:
                session = 1 if slot < point.n_total_slots // 2 else 2
            else:
                session = 3
            requested[meeting] = (pair[0], pair[1], session)
    finalized_requested = [value for value in requested if value is not None]
    if len(finalized_requested) != point.n_meetings:
        raise AssertionError("meeting construction is incomplete")

    meetings_by_business = [[] for _ in range(point.n_business)]
    for meeting, (left, right, _) in enumerate(finalized_requested):
        meetings_by_business[left].append(meeting)
        meetings_by_business[right].append(meeting)

    fixed = [None] * point.n_meetings
    fixed_count = round(point.fixed_ratio * point.n_meetings)
    for meeting in rng.sample(range(point.n_meetings), fixed_count):
        fixed[meeting] = assignment[meeting]

    forbidden: list[set[int]] = []
    for meetings in meetings_by_business:
        busy = {assignment[meeting] for meeting in meetings}
        candidates = [
            slot for slot in range(point.n_total_slots) if slot not in busy
        ]
        count = min(
            len(candidates),
            round(point.forbidden_ratio * point.n_total_slots),
        )
        forbidden.append(set(rng.sample(candidates, count)))

    precedences = [set() for _ in range(point.n_meetings)]
    candidate_edges: list[tuple[int, int]] = []
    for meetings in meetings_by_business:
        ordered = sorted(meetings, key=lambda value: (assignment[value], value))
        for position, post in enumerate(ordered[1:], start=1):
            possible = [
                pred
                for pred in ordered[:position]
                if assignment[pred] < assignment[post]
            ]
            if not possible:
                continue
            possible.sort(
                key=lambda pred: (
                    assignment[post] - assignment[pred],
                    pred,
                )
            )
            # Low depth selects nearby predecessors; high depth selects farther
            # predecessors, increasing the chance of long closure chains.
            rank = round(point.precedence_depth * (len(possible) - 1))
            candidate_edges.append((possible[rank], post))
    candidate_edges = sorted(set(candidate_edges))
    rng.shuffle(candidate_edges)
    precedence_candidate_edges = len(candidate_edges)
    target_edges = round(point.precedence_density * precedence_candidate_edges)
    for predecessor, post in candidate_edges[:target_edges]:
        precedences[post].add(predecessor)

    instance = B2BInstance(
        n_business=point.n_business,
        n_meetings=point.n_meetings,
        n_tables=n_tables,
        n_total_slots=point.n_total_slots,
        n_morning_slots=point.n_total_slots // 2,
        requested=finalized_requested,
        meetings_by_business=meetings_by_business,
        n_meetings_business=list(map(len, meetings_by_business)),
        forbidden=forbidden,
        fixed=fixed,
        precedences=precedences,
        instance_name="",
    )
    errors = validate_schedule_assignment(instance, assignment)
    if errors:
        raise RuntimeError(f"constructed witness is invalid: {errors[:3]}")
    degree_total = sum(instance.n_meetings_business)
    ordered_degrees = sorted(instance.n_meetings_business)
    degree_gini = 0.0
    if degree_total:
        degree_gini = (
            2
            * sum(
                (index + 1) * degree
                for index, degree in enumerate(ordered_degrees)
            )
            / (instance.n_business * degree_total)
            - (instance.n_business + 1) / instance.n_business
        )
    diagnostics = {
        "precedence_candidate_edges": precedence_candidate_edges,
        "precedence_density_realized": (
            target_edges / precedence_candidate_edges
            if precedence_candidate_edges
            else 0.0
        ),
        "participant_degree_gini_realized": degree_gini,
        "session_restricted_ratio_realized": (
            sum(session != 3 for _, _, session in instance.requested)
            / instance.n_meetings
        ),
    }
    return instance, assignment, diagnostics


def _format_set(values: Iterable[int], *, offset: int = 1, empty_zero: bool = False) -> str:
    converted = [value + offset for value in sorted(values)]
    if not converted and empty_zero:
        converted = [0]
    return "{" + ",".join(map(str, converted)) + "}"


def render_instance(
    instance: B2BInstance,
    *,
    name: str,
    point: DesignPoint,
    witness_sha256: str,
) -> str:
    requested = "\n".join(
        (
            "requested = [|"
            if meeting == 0
            else "|"
        )
        + f"{left + 1}, {right + 1},{session}, "
        for meeting, (left, right, session) in enumerate(instance.requested)
    )
    requested += "\n|];"
    # Original instances reserve value 1 as a dummy and number real meetings
    # from 2. Preserve that redundant convention so the strict parser can
    # audit meetingsxBusiness against ``requested``.
    meetings_sets = [
        _format_set({0, *(meeting + 1 for meeting in meetings)}, offset=1)
        for meetings in instance.meetings_by_business
    ]
    forbidden_sets = [
        _format_set(values, offset=1, empty_zero=True)
        for values in instance.forbidden
    ]
    precedence_sets = [
        _format_set(values, offset=1)
        for values in instance.precedences
    ]
    fixed = [0 if value is None else value + 1 for value in instance.fixed]
    header = [
        "% Generated structural B2B benchmark; not part of the official archive.",
        f"% generator_schema_version={SCHEMA_VERSION}",
        f"% split={point.split}",
        f"% generator_seed={point.seed}",
        f"% structural_stratum_id={point.stratum_id}",
        f"% witness_sha256={witness_sha256}",
        f"% instance={name}",
        f"nBusiness = {instance.n_business};",
        f"nMeetings = {instance.n_meetings};",
        f"nTables = {instance.n_tables};",
        f"nTotalSlots = {instance.n_total_slots};",
        f"nMorningSlots = {instance.n_morning_slots};",
        "",
        requested,
        "",
        "meetingsxBusiness = ["
        + ",\n".join(meetings_sets)
        + "];",
        "",
        "nMeetingsBusiness = ["
        + ",".join(map(str, instance.n_meetings_business))
        + "];",
        "",
        "forbidden = [" + ",\n".join(forbidden_sets) + "];",
        "",
        "fixed = [" + ",".join(map(str, fixed)) + "];",
        "",
        "precedences = [" + ",\n".join(precedence_sets) + "];",
        "",
    ]
    return "\n".join(header)


def _manifest_row(
    path: Path,
    instance: B2BInstance,
    *,
    digest: str,
    lineage_id: str,
) -> dict[str, Any]:
    return {
        "content_id": f"b2b-generated-{digest[:16]}",
        "base_lineage_id": lineage_id,
        "sha256": digest,
        "canonical_instance": path.stem,
        "canonical_run_path": path.name,
        "family": "generated",
        "variant": "structural",
        "source_alias_count": 1,
        "source_alias_paths": path.name,
        "repository_alias_count": 1,
        "repository_alias_paths": path.name,
        "n_business": instance.n_business,
        "n_meetings": instance.n_meetings,
        "n_tables": instance.n_tables,
        "n_total_slots": instance.n_total_slots,
        "n_morning_slots": instance.n_morning_slots,
        "n_forbidden_assignments": sum(map(len, instance.forbidden)),
        "n_fixed_meetings": sum(value is not None for value in instance.fixed),
        "n_direct_precedence_edges": sum(map(len, instance.precedences)),
        "dataset_source_page": "",
        "dataset_archive_url": "",
        "dataset_archive_sha256": "",
    }


def generate_dataset(
    output_dir: Path,
    *,
    n_development: int,
    n_heldout: int,
    master_seed: int,
) -> None:
    if output_dir.exists():
        raise FileExistsError(
            f"output directory already exists: {output_dir}; generation never overwrites"
        )
    output_dir.mkdir(parents=True)
    points = [
        *build_design(
            split="development",
            count=n_development,
            master_seed=master_seed,
        ),
        *build_design(
            split="heldout",
            count=n_heldout,
            master_seed=master_seed,
        ),
    ]
    manifest_rows: list[dict[str, Any]] = []
    split_rows: dict[str, list[dict[str, Any]]] = {
        "development": [],
        "heldout": [],
    }
    generation_rows: list[dict[str, Any]] = []
    witness_rows: list[dict[str, Any]] = []
    for global_index, point in enumerate(points):
        instance, assignment, diagnostics = _construct_witness(point)
        prefix = "dev" if point.split == "development" else "heldout"
        name = f"journal-{prefix}-{point.index:04d}"
        lineage_id = f"b2b-generated-lineage-{prefix}-{point.index:04d}"
        witness_sha256 = _assignment_sha256(assignment)
        output_path = output_dir / f"{name}.dzn"
        output_path.write_text(
            render_instance(
                instance,
                name=name,
                point=point,
                witness_sha256=witness_sha256,
            ),
            encoding="utf-8",
        )
        parsed = read_instance(output_path)
        errors = validate_schedule_assignment(parsed, assignment)
        if errors:
            raise RuntimeError(f"rendered witness invalid for {name}: {errors[:3]}")
        digest = file_sha256(output_path)
        manifest_row = _manifest_row(
            output_path,
            parsed,
            digest=digest,
            lineage_id=lineage_id,
        )
        manifest_rows.append(manifest_row)
        split_rows[point.split].append(manifest_row)
        realized_pressure = (
            parsed.n_meetings / (parsed.n_tables * parsed.n_total_slots)
        )
        generation_rows.append(
            {
                "instance": output_path.stem,
                "content_id": manifest_row["content_id"],
                "sha256": digest,
                "split": point.split,
                "generator_seed": point.seed,
                "base_generator_seed": point.seed,
                "base_lineage_id": lineage_id,
                "structural_stratum_id": point.stratum_id,
                "requested_n_meetings": point.n_meetings,
                "n_meetings": parsed.n_meetings,
                "n_business": parsed.n_business,
                "n_tables": parsed.n_tables,
                "n_total_slots": parsed.n_total_slots,
                "capacity_pressure_requested": round(point.capacity_pressure, 6),
                "capacity_pressure_realized": round(realized_pressure, 6),
                "fixed_ratio_requested": round(point.fixed_ratio, 6),
                "fixed_ratio_realized": round(
                    sum(value is not None for value in parsed.fixed)
                    / parsed.n_meetings,
                    6,
                ),
                "forbidden_ratio_requested": round(point.forbidden_ratio, 6),
                "forbidden_ratio_realized": round(
                    sum(map(len, parsed.forbidden))
                    / (parsed.n_business * parsed.n_total_slots),
                    6,
                ),
                "precedence_density_requested": round(point.precedence_density, 6),
                "precedence_density_realized": round(
                    diagnostics["precedence_density_realized"],
                    6,
                ),
                "precedence_depth_requested": round(point.precedence_depth, 6),
                "precedence_candidate_edges": diagnostics[
                    "precedence_candidate_edges"
                ],
                "precedence_direct_edges": sum(map(len, parsed.precedences)),
                "participant_degree_skew_requested": round(point.degree_skew, 6),
                "participant_degree_gini_realized": round(
                    diagnostics["participant_degree_gini_realized"],
                    6,
                ),
                "session_restricted_ratio_requested": round(
                    point.session_restricted_ratio,
                    6,
                ),
                "session_restricted_ratio_realized": round(
                    diagnostics["session_restricted_ratio_realized"],
                    6,
                ),
                "witness_sha256": witness_sha256,
            }
        )
        witness_rows.append(
            {
                "instance": output_path.stem,
                "split": point.split,
                "base_lineage_id": lineage_id,
                "witness_sha256": witness_sha256,
                "assignment_1_based": " ".join(
                    str(slot + 1) for slot in assignment
                ),
            }
        )
        print(
            f"[{global_index + 1}/{len(points)}] {name} "
            f"M={parsed.n_meetings} P={parsed.n_business} "
            f"T={parsed.n_total_slots}",
            flush=True,
        )
    _write_csv(output_dir / "instances_manifest.csv", MANIFEST_FIELDS, manifest_rows)
    _write_csv(
        output_dir / "development_manifest.csv",
        MANIFEST_FIELDS,
        split_rows["development"],
    )
    _write_csv(
        output_dir / "heldout_manifest.csv",
        MANIFEST_FIELDS,
        split_rows["heldout"],
    )
    _write_csv(output_dir / "generation_manifest.csv", GENERATION_FIELDS, generation_rows)
    _write_csv(output_dir / "witnesses.csv", WITNESS_FIELDS, witness_rows)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "construction": "witness_first_hybrid_maximin_lhs_quota_strata",
        "lhs_candidate_designs": 32,
        "master_seed": master_seed,
        "n_development": n_development,
        "n_heldout": n_heldout,
        "heldout_policy": (
            "heldout seeds are frozen before solver runs and must not be used "
            "for generator, timeout, feature, or model tuning"
        ),
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_dataset(output_dir: Path) -> list[str]:
    errors: list[str] = []
    with (output_dir / "generation_manifest.csv").open(
        newline="",
        encoding="utf-8",
    ) as stream:
        generation_rows = list(csv.DictReader(stream))
    with (output_dir / "witnesses.csv").open(
        newline="",
        encoding="utf-8",
    ) as stream:
        witnesses = {row["instance"]: row for row in csv.DictReader(stream)}
    manifest_sets: dict[str, set[str]] = {}
    for name in (
        "instances_manifest.csv",
        "development_manifest.csv",
        "heldout_manifest.csv",
    ):
        with (output_dir / name).open(newline="", encoding="utf-8") as stream:
            manifest_sets[name] = {
                row["content_id"] for row in csv.DictReader(stream)
            }
    try:
        metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid metadata.json: {exc}")
        metadata = {}
    hashes: set[str] = set()
    lineages: set[str] = set()
    split_seeds: dict[str, set[str]] = {"development": set(), "heldout": set()}
    for row in generation_rows:
        instance_path = output_dir / f"{row['instance']}.dzn"
        if not instance_path.is_file():
            errors.append(f"missing instance: {instance_path.name}")
            continue
        digest = file_sha256(instance_path)
        if digest != row["sha256"]:
            errors.append(f"SHA-256 mismatch: {instance_path.name}")
        if digest in hashes:
            errors.append(f"duplicate content hash: {digest}")
        hashes.add(digest)
        expected_content_id = f"b2b-generated-{digest[:16]}"
        if row["content_id"] != expected_content_id:
            errors.append(f"content_id mismatch: {row['instance']}")
        if row["split"] not in split_seeds:
            errors.append(f"invalid split: {row['instance']}")
            continue
        lineage = row["base_lineage_id"]
        if lineage in lineages:
            errors.append(f"duplicate generated lineage: {lineage}")
        lineages.add(lineage)
        split_seeds[row["split"]].add(row["base_generator_seed"])
        witness = witnesses.get(row["instance"])
        if witness is None:
            errors.append(f"missing witness: {row['instance']}")
            continue
        for field in ("split", "base_lineage_id", "witness_sha256"):
            if witness[field] != row[field]:
                errors.append(
                    f"witness/generation mismatch for {row['instance']}/{field}"
                )
        assignment = [
            int(value) - 1 for value in witness["assignment_1_based"].split()
        ]
        if _assignment_sha256(assignment) != witness["witness_sha256"]:
            errors.append(f"witness hash mismatch: {row['instance']}")
        parsed = read_instance(instance_path)
        schedule_errors = validate_schedule_assignment(parsed, assignment)
        if schedule_errors:
            errors.append(
                f"invalid witness {row['instance']}: {schedule_errors[:2]}"
            )
    leakage = split_seeds["development"].intersection(split_seeds["heldout"])
    if leakage:
        errors.append(f"development/heldout seed leakage: {sorted(leakage)[:3]}")
    generated_contents = {row["content_id"] for row in generation_rows}
    expected_split_contents = {
        split: {
            row["content_id"] for row in generation_rows if row["split"] == split
        }
        for split in ("development", "heldout")
    }
    if manifest_sets["instances_manifest.csv"] != generated_contents:
        errors.append("instances_manifest.csv does not match generation manifest")
    for split in ("development", "heldout"):
        if (
            manifest_sets[f"{split}_manifest.csv"]
            != expected_split_contents[split]
        ):
            errors.append(f"{split}_manifest.csv has wrong contents")
        expected_count = int(metadata.get(f"n_{split}", -1))
        if len(expected_split_contents[split]) != expected_count:
            errors.append(
                f"metadata {split} count mismatch: "
                f"{expected_count}!={len(expected_split_contents[split])}"
            )
    dzn_names = {path.stem for path in output_dir.glob("*.dzn")}
    generation_names = {row["instance"] for row in generation_rows}
    if dzn_names != generation_names:
        errors.append(".dzn file set does not match generation manifest")
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or validate the journal structural B2B benchmark."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output-dir", default="data_journal_generated")
    generate.add_argument("--n-development", type=int, default=240)
    generate.add_argument("--n-heldout", type=int, default=60)
    generate.add_argument("--master-seed", type=int, default=DEFAULT_MASTER_SEED)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--data-dir", default="data_journal_generated")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "generate":
        if args.n_development <= 0 or args.n_heldout <= 0:
            print("ERROR: both split sizes must be positive")
            return 2
        generate_dataset(
            Path(args.output_dir),
            n_development=args.n_development,
            n_heldout=args.n_heldout,
            master_seed=args.master_seed,
        )
        return 0
    errors = validate_dataset(Path(args.data_dir))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"validated generated dataset: {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
