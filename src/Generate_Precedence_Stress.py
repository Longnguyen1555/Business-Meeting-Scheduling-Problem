from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from pysat import __version__ as pysat_version
from pysat.solvers import Solver

from B2B_Instance import (
    B2BInstance,
    B2BSATModel,
    build_precedence_graph,
    read_instance,
    validate_schedule_assignment,
)
from Dataset_Manifest import MANIFEST_FIELDS, base_lineage_id, file_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIRECTORY = PROJECT_ROOT / "data_table03_origin"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "data_precedence_stress"
DEFAULT_GAMMAS = (30, 35, 40)
DEFAULT_GLOBAL_SEED = 20260724
GENERATOR_SCHEMA_VERSION = 1
CONSTRUCTION_NAME = "nested_witness_consistent_precedence_dag"

WITNESS_FIELDS = (
    "source_instance",
    "source_sha256",
    "n_meetings",
    "n_total_slots",
    "sat_backend",
    "python_sat_version",
    "canonicalization",
    "witness_sha256",
    "assignment_1_based",
)

GENERATION_MANIFEST_FIELDS = (
    "instance",
    "generated_sha256",
    "source_instance",
    "source_sha256",
    "gamma_percent",
    "global_seed",
    "instance_seed_hex",
    "target_direct_edges",
    "actual_direct_edges",
    "transitive_edges",
    "max_chain_distance",
    "meeting_incidences",
    "realized_incidence_density_percent",
    "eligible_precedence_posts",
    "realized_eligible_post_density_percent",
    "witness_sha256",
    "construction",
)


def _write_csv(
    path: Path,
    fieldnames: Iterable[str],
    rows: Iterable[dict[str, Any]],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _assignment_sha256(assignment: list[int]) -> str:
    payload = ",".join(str(slot + 1) for slot in assignment).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _instance_seed_hex(global_seed: int, source_sha256: str) -> str:
    payload = f"{global_seed}:{source_sha256}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()[:16]


def _participant_rng(
    global_seed: int,
    source_sha256: str,
    participant: int,
) -> random.Random:
    payload = f"{global_seed}:{source_sha256}:participant:{participant}".encode(
        "ascii"
    )
    seed = int(hashlib.sha256(payload).hexdigest()[:16], 16)
    return random.Random(seed)


def _validate_gamma_levels(gammas: Iterable[int]) -> tuple[int, ...]:
    levels = tuple(sorted(set(gammas)))
    if not levels:
        raise ValueError("At least one gamma level is required")
    if any(gamma < 0 or gamma >= 100 for gamma in levels):
        raise ValueError(
            "Witness-preserving gamma levels must satisfy 0 <= gamma < 100"
        )
    return levels


def _canonical_witness(
    inst: B2BInstance,
    *,
    sat_backend: str,
) -> list[int]:
    """Return the lexicographically smallest feasible slot vector.

    Repeated SAT assumptions choose the smallest feasible slot for each meeting
    in meeting-id order. The witness is therefore independent of SAT branching
    choices as long as the backend answers SAT/UNSAT correctly.
    """

    if any(inst.precedences):
        raise ValueError(
            f"{inst.instance_name} is not an original zero-precedence instance"
        )

    model = B2BSATModel(
        inst,
        precedence_encoding="pairwise",
        precedence_graph="direct",
        encoding_variant="imp12+",
        domain_mode="reduced",
    )
    artifacts = model.build_base_cnf()
    chosen_literals: list[int] = []
    assignment: list[int] = []

    with Solver(
        name=sat_backend,
        bootstrap_with=artifacts.cnf.clauses,
    ) as solver:
        if not solver.solve():
            raise RuntimeError(
                f"Cannot construct a feasible witness for {inst.instance_name}"
            )

        for meeting in range(inst.n_meetings):
            for slot in model.reduced_slots(meeting):
                literal = model.x(meeting, slot)
                if solver.solve(assumptions=[*chosen_literals, literal]):
                    chosen_literals.append(literal)
                    assignment.append(slot)
                    break
            else:
                raise RuntimeError(
                    "Canonical witness construction lost feasibility at "
                    f"meeting {meeting + 1} in {inst.instance_name}"
                )

    errors = validate_schedule_assignment(inst, assignment)
    if errors:
        raise RuntimeError(
            f"Invalid canonical witness for {inst.instance_name}: {errors[:3]}"
        )
    return assignment


def _participant_edge_ladders(
    inst: B2BInstance,
    assignment: list[int],
    *,
    global_seed: int,
    source_sha256: str,
) -> list[list[tuple[int, int]]]:
    """Create one nested, randomized edge ladder per participant.

    Each ladder contains at most one candidate incoming arc per later meeting.
    An arc is excluded when its two meetings share both participants, because
    such an arc would ambiguously consume two per-participant edge budgets.
    All retained arcs point from an earlier witness slot to a later witness
    slot.
    """

    ladders: list[list[tuple[int, int]]] = []
    participants_by_meeting = [set() for _ in range(inst.n_meetings)]
    for participant, meetings in enumerate(inst.meetings_by_business):
        for meeting in meetings:
            participants_by_meeting[meeting].add(participant)

    for participant, meetings in enumerate(inst.meetings_by_business):
        ordered = sorted(meetings, key=lambda meeting: (assignment[meeting], meeting))
        if any(
            assignment[left] == assignment[right]
            for left, right in zip(ordered, ordered[1:])
        ):
            raise ValueError(
                f"Witness collision for participant {participant + 1}"
            )

        posts = ordered[1:]
        rng = _participant_rng(global_seed, source_sha256, participant)
        rng.shuffle(posts)
        edges: list[tuple[int, int]] = []
        for post in posts:
            predecessors = [
                meeting
                for meeting in ordered
                if assignment[meeting] < assignment[post]
                and (
                    participants_by_meeting[meeting]
                    & participants_by_meeting[post]
                )
                == {participant}
            ]
            if not predecessors:
                continue
            pred = rng.choice(predecessors)
            edges.append((pred, post))
        ladders.append(edges)

    return ladders


def _precedences_for_gamma(
    inst: B2BInstance,
    ladders: list[list[tuple[int, int]]],
    gamma: int,
) -> tuple[list[set[int]], int]:
    """Apply the edge budget used by the official prec15/prec25 files.

    For participant p with d_p meetings, the budget is
    floor(gamma*d_p/100). Summing this quantity exactly reproduces the direct
    edge counts in every official prec15 and prec25 instance.
    """

    precedences = [set() for _ in range(inst.n_meetings)]
    target = 0
    selected_edges: set[tuple[int, int]] = set()

    for meetings, ladder in zip(inst.meetings_by_business, ladders):
        edge_budget = math.floor(gamma * len(meetings) / 100)
        if edge_budget > len(ladder):
            raise ValueError(
                f"gamma={gamma} cannot preserve a witness for a participant "
                f"with {len(meetings)} meetings"
            )
        target += edge_budget
        for pred, post in ladder[:edge_budget]:
            edge = (pred, post)
            if edge in selected_edges:
                raise ValueError(f"Duplicate generated precedence edge {edge}")
            selected_edges.add(edge)
            precedences[post].add(pred)

    actual = sum(len(preds) for preds in precedences)
    if actual != target:
        raise AssertionError(
            f"Expected {target} direct edges at gamma={gamma}, got {actual}"
        )
    return precedences, target


def _format_precedence_assignment(precedences: list[set[int]]) -> str:
    sets = []
    for predecessors in precedences:
        values = ", ".join(str(pred + 1) for pred in sorted(predecessors))
        sets.append("{" + values + "}")
    return "precedences = [\n" + ",\n".join(sets) + "\n];"


def _replace_precedence_assignment(
    source_text: str,
    precedences: list[set[int]],
) -> str:
    match = re.search(r"\bprecedences\s*=\s*\[", source_text)
    if not match:
        raise ValueError("Cannot find precedences assignment")

    assignment_start = match.start()
    bracket_start = match.end() - 1
    depth = 0
    bracket_end: int | None = None
    for index in range(bracket_start, len(source_text)):
        if source_text[index] == "[":
            depth += 1
        elif source_text[index] == "]":
            depth -= 1
            if depth == 0:
                bracket_end = index
                break
    if bracket_end is None:
        raise ValueError("Cannot find end of precedences assignment")

    semicolon = bracket_end + 1
    while semicolon < len(source_text) and source_text[semicolon].isspace():
        semicolon += 1
    if semicolon >= len(source_text) or source_text[semicolon] != ";":
        raise ValueError("Precedences assignment is not terminated by ';'")

    replacement = _format_precedence_assignment(precedences)
    return source_text[:assignment_start] + replacement + source_text[semicolon + 1 :]


def _render_instance(
    source_text: str,
    *,
    source_name: str,
    source_sha256: str,
    gamma: int,
    global_seed: int,
    instance_seed_hex: str,
    witness_sha256: str,
    precedences: list[set[int]],
) -> str:
    header = "\n".join(
        (
            "% Derived precedence-density stress-test instance.",
            f"% source_instance={source_name}",
            f"% source_sha256={source_sha256}",
            f"% gamma_percent={gamma}",
            f"% global_seed={global_seed}",
            f"% instance_seed_hex={instance_seed_hex}",
            f"% witness_sha256={witness_sha256}",
            f"% construction={CONSTRUCTION_NAME}",
            "% Not part of the official UdG b2b.zip benchmark archive.",
            "",
        )
    )
    body = _replace_precedence_assignment(source_text, precedences)
    normalized_lines = [line.rstrip() for line in body.splitlines()]
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()
    return header + "\n".join(normalized_lines).lstrip() + "\n"


def _base_name(source_path: Path) -> str:
    suffix = ".original.dzn"
    if not source_path.name.endswith(suffix):
        raise ValueError(f"Unexpected original instance name {source_path.name}")
    return source_path.name[: -len(suffix)]


def _runner_manifest_row(
    output_path: Path,
    parsed: B2BInstance,
    *,
    gamma: int,
    digest: str,
) -> dict[str, Any]:
    return {
        "content_id": f"b2b-stress-{digest[:16]}",
        "base_lineage_id": base_lineage_id(output_path.name),
        "sha256": digest,
        "canonical_instance": output_path.stem,
        "canonical_run_path": output_path.name,
        "family": "precedence",
        "variant": f"prec{gamma}",
        "source_alias_count": 1,
        "source_alias_paths": output_path.name,
        "repository_alias_count": 1,
        "repository_alias_paths": output_path.name,
        "n_business": parsed.n_business,
        "n_meetings": parsed.n_meetings,
        "n_tables": parsed.n_tables,
        "n_total_slots": parsed.n_total_slots,
        "n_morning_slots": parsed.n_morning_slots,
        "n_forbidden_assignments": sum(
            len(slots) for slots in parsed.forbidden
        ),
        "n_fixed_meetings": sum(slot is not None for slot in parsed.fixed),
        "n_direct_precedence_edges": sum(
            len(predecessors) for predecessors in parsed.precedences
        ),
        "dataset_source_page": "",
        "dataset_archive_url": "",
        "dataset_archive_sha256": "",
    }


def _dataset_readme(
    gammas: tuple[int, ...],
    global_seed: int,
    source_count: int,
    dataset_directory_name: str,
) -> str:
    gamma_text = ", ".join(f"`prec{gamma}`" for gamma in gammas)
    gamma_arguments = " ".join(str(gamma) for gamma in gammas)
    return f"""# Derived precedence-density stress dataset

This directory contains {source_count * len(gammas)} derived instances:
{source_count} original B2B contents at each of {gamma_text}. These files are
not part of the official UdG `b2b.zip` archive.

## Construction

For each original instance, the generator first computes the
lexicographically smallest feasible meeting-slot vector using incremental SAT
assumptions. Every generated precedence arc points from an earlier witness slot
to a later witness slot, so the resulting graph is acyclic and the recorded
witness remains feasible.

For a participant with `d` meetings and requested density `gamma`, exactly
`floor(gamma*d/100)` incoming precedence arcs are selected. This is the edge
budget that reproduces the direct-edge counts of every official `prec15` and
`prec25` file. The randomized edge ladders use global seed `{global_seed}` and
are nested: every lower-density edge remains present at all higher levels.

`gamma` is therefore a requested per-participant density parameter. Because of
integer rounding, the realized aggregate density is also recorded explicitly
in `generation_manifest.csv`.

## Files

- `witnesses.csv`: canonical feasible schedules and source hashes.
- `generation_manifest.csv`: generation parameters and structural statistics.
- `instances_manifest.csv`: runner-compatible manifest with blank official
  archive attribution.
- `metadata.json`: dataset-level parameters.

## Reproduce and validate

From the repository root:

```bash
python3 src/Generate_Precedence_Stress.py generate \\
  --output-dir {dataset_directory_name} \\
  --gammas {gamma_arguments}

python3 src/Generate_Precedence_Stress.py validate \\
  --data-dir {dataset_directory_name}
```

The generator refuses to overwrite an existing output directory.

## Pilot run

The controlled four-cell precedence pilot contains
`{source_count * len(gammas) * 4}` runs:

```bash
python3 src/Main.py \\
  --manifest {dataset_directory_name}/instances_manifest.csv \\
  --family precedence \\
  --solver maxsat \\
  --maxsat-backend uwrmaxsat \\
  --uwrmaxsat-bin /absolute/path/to/uwrmaxsat \\
  --domain-mode reduced \\
  --precedence-encoding both \\
  --precedence-graph both \\
  --encoding-variant imp12+ \\
  --timeout 7200 \\
  --csv output/{dataset_directory_name}_pilot.csv
```
"""


def _generate_into_directory(
    source_directory: Path,
    destination: Path,
    *,
    dataset_directory_name: str,
    gammas: tuple[int, ...],
    global_seed: int,
    sat_backend: str,
) -> None:
    source_paths = sorted(source_directory.glob("*.original.dzn"))
    if not source_paths:
        raise FileNotFoundError(
            f"No .original.dzn files found in {source_directory}"
        )

    witness_rows: list[dict[str, Any]] = []
    generation_rows: list[dict[str, Any]] = []
    runner_rows: list[dict[str, Any]] = []

    for source_path in source_paths:
        source_sha256 = file_sha256(source_path)
        source_text = source_path.read_text(encoding="utf-8")
        source = read_instance(source_path)
        if any(source.precedences):
            raise ValueError(
                f"Source {source_path.name} already contains precedences"
            )

        assignment = _canonical_witness(source, sat_backend=sat_backend)
        witness_sha256 = _assignment_sha256(assignment)
        instance_seed_hex = _instance_seed_hex(global_seed, source_sha256)
        ladders = _participant_edge_ladders(
            source,
            assignment,
            global_seed=global_seed,
            source_sha256=source_sha256,
        )
        witness_rows.append(
            {
                "source_instance": source_path.name,
                "source_sha256": source_sha256,
                "n_meetings": source.n_meetings,
                "n_total_slots": source.n_total_slots,
                "sat_backend": sat_backend,
                "python_sat_version": pysat_version,
                "canonicalization": (
                    "lexicographically_smallest_slot_vector_by_meeting_id"
                ),
                "witness_sha256": witness_sha256,
                "assignment_1_based": " ".join(
                    str(slot + 1) for slot in assignment
                ),
            }
        )

        for gamma in gammas:
            precedences, target_edges = _precedences_for_gamma(
                source,
                ladders,
                gamma,
            )
            output_path = destination / f"{_base_name(source_path)}.prec{gamma}.dzn"
            output_text = _render_instance(
                source_text,
                source_name=source_path.name,
                source_sha256=source_sha256,
                gamma=gamma,
                global_seed=global_seed,
                instance_seed_hex=instance_seed_hex,
                witness_sha256=witness_sha256,
                precedences=precedences,
            )
            output_path.write_text(output_text, encoding="utf-8")

            parsed = read_instance(output_path)
            errors = validate_schedule_assignment(parsed, assignment)
            if errors:
                raise RuntimeError(
                    f"Generated witness failed for {output_path.name}: {errors[:3]}"
                )
            graph = build_precedence_graph(parsed.precedences)
            if graph.cycle_nodes:
                raise RuntimeError(
                    f"Generated cycle in {output_path.name}: {graph.cycle_nodes}"
                )

            digest = file_sha256(output_path)
            meeting_incidences = sum(source.n_meetings_business)
            eligible_posts = sum(
                max(0, meeting_count - 1)
                for meeting_count in source.n_meetings_business
            )
            generation_rows.append(
                {
                    "instance": output_path.name,
                    "generated_sha256": digest,
                    "source_instance": source_path.name,
                    "source_sha256": source_sha256,
                    "gamma_percent": gamma,
                    "global_seed": global_seed,
                    "instance_seed_hex": instance_seed_hex,
                    "target_direct_edges": target_edges,
                    "actual_direct_edges": graph.direct_edge_count,
                    "transitive_edges": graph.transitive_edge_count,
                    "max_chain_distance": graph.max_chain_distance,
                    "meeting_incidences": meeting_incidences,
                    "realized_incidence_density_percent": (
                        f"{100 * graph.direct_edge_count / meeting_incidences:.6f}"
                    ),
                    "eligible_precedence_posts": eligible_posts,
                    "realized_eligible_post_density_percent": (
                        f"{100 * graph.direct_edge_count / eligible_posts:.6f}"
                    ),
                    "witness_sha256": witness_sha256,
                    "construction": CONSTRUCTION_NAME,
                }
            )
            runner_rows.append(
                _runner_manifest_row(
                    output_path,
                    parsed,
                    gamma=gamma,
                    digest=digest,
                )
            )

    _write_csv(destination / "witnesses.csv", WITNESS_FIELDS, witness_rows)
    _write_csv(
        destination / "generation_manifest.csv",
        GENERATION_MANIFEST_FIELDS,
        generation_rows,
    )
    _write_csv(
        destination / "instances_manifest.csv",
        MANIFEST_FIELDS,
        runner_rows,
    )
    metadata = {
        "schema_version": GENERATOR_SCHEMA_VERSION,
        "construction": CONSTRUCTION_NAME,
        "gamma_levels": list(gammas),
        "global_seed": global_seed,
        "source_directory": source_directory.name,
        "source_instance_count": len(source_paths),
        "generated_instance_count": len(generation_rows),
        "witness_method": (
            "lexicographically_smallest_slot_vector_by_meeting_id"
        ),
        "edge_budget": "sum_p floor(gamma * degree(p) / 100)",
        "official_archive_member": False,
    }
    (destination / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (destination / "README.md").write_text(
        _dataset_readme(
            gammas,
            global_seed,
            len(source_paths),
            dataset_directory_name,
        ),
        encoding="utf-8",
    )


def _parse_assignment(row: dict[str, str]) -> list[int]:
    return [
        int(value) - 1
        for value in row["assignment_1_based"].split()
    ]


def validate_dataset(
    data_directory: str | Path,
    source_directory: str | Path = DEFAULT_SOURCE_DIRECTORY,
) -> dict[str, int]:
    data_root = Path(data_directory).resolve()
    source_root = Path(source_directory).resolve()
    metadata = json.loads(
        (data_root / "metadata.json").read_text(encoding="utf-8")
    )
    gammas = _validate_gamma_levels(
        int(gamma) for gamma in metadata["gamma_levels"]
    )
    global_seed = int(metadata["global_seed"])

    source_paths = sorted(source_root.glob("*.original.dzn"))
    sources = {path.name: (path, read_instance(path)) for path in source_paths}
    witness_rows = _read_csv(data_root / "witnesses.csv")
    generation_rows = _read_csv(data_root / "generation_manifest.csv")
    runner_rows = _read_csv(data_root / "instances_manifest.csv")

    if len(witness_rows) != len(sources):
        raise ValueError(
            f"Expected {len(sources)} witnesses, found {len(witness_rows)}"
        )
    expected_generated_count = len(sources) * len(gammas)
    if len(generation_rows) != expected_generated_count:
        raise ValueError(
            f"Expected {expected_generated_count} generated rows, "
            f"found {len(generation_rows)}"
        )
    if len(runner_rows) != expected_generated_count:
        raise ValueError(
            f"Expected {expected_generated_count} runner rows, "
            f"found {len(runner_rows)}"
        )

    witnesses: dict[str, tuple[list[int], str]] = {}
    for row in witness_rows:
        source_name = row["source_instance"]
        if source_name not in sources:
            raise ValueError(f"Unknown witness source {source_name}")
        source_path, source = sources[source_name]
        source_sha256 = file_sha256(source_path)
        if row["source_sha256"] != source_sha256:
            raise ValueError(f"Source hash mismatch for {source_name}")
        assignment = _parse_assignment(row)
        if len(assignment) != source.n_meetings:
            raise ValueError(f"Witness length mismatch for {source_name}")
        witness_sha256 = _assignment_sha256(assignment)
        if row["witness_sha256"] != witness_sha256:
            raise ValueError(f"Witness hash mismatch for {source_name}")
        errors = validate_schedule_assignment(source, assignment)
        if errors:
            raise ValueError(
                f"Invalid source witness for {source_name}: {errors[:3]}"
            )
        witnesses[source_name] = (assignment, witness_sha256)

    generated_by_source: dict[str, dict[int, set[tuple[int, int]]]] = {}
    manifest_names: set[str] = set()
    for row in generation_rows:
        instance_name = row["instance"]
        if instance_name in manifest_names:
            raise ValueError(f"Duplicate generation row for {instance_name}")
        manifest_names.add(instance_name)
        source_name = row["source_instance"]
        if source_name not in sources:
            raise ValueError(f"Unknown generated source {source_name}")
        gamma = int(row["gamma_percent"])
        if gamma not in gammas:
            raise ValueError(f"Unexpected gamma={gamma} in {instance_name}")
        if int(row["global_seed"]) != global_seed:
            raise ValueError(f"Global seed mismatch in {instance_name}")

        generated_path = data_root / instance_name
        if not generated_path.is_file():
            raise FileNotFoundError(generated_path)
        digest = file_sha256(generated_path)
        if row["generated_sha256"] != digest:
            raise ValueError(f"Generated hash mismatch for {instance_name}")

        source_path, source = sources[source_name]
        if row["source_sha256"] != file_sha256(source_path):
            raise ValueError(f"Source hash mismatch in {instance_name}")
        assignment, witness_sha256 = witnesses[source_name]
        if row["witness_sha256"] != witness_sha256:
            raise ValueError(f"Witness reference mismatch in {instance_name}")

        generated = read_instance(generated_path)
        restored = replace(
            generated,
            precedences=source.precedences,
            instance_name=source.instance_name,
        )
        if restored != source:
            raise ValueError(
                f"Non-precedence data changed in {instance_name}"
            )

        graph = build_precedence_graph(generated.precedences)
        if graph.cycle_nodes:
            raise ValueError(
                f"Precedence cycle in {instance_name}: {graph.cycle_nodes}"
            )
        errors = validate_schedule_assignment(
            generated,
            assignment,
            graph=graph,
        )
        if errors:
            raise ValueError(
                f"Witness invalid for {instance_name}: {errors[:3]}"
            )

        edges = {
            (pred, post)
            for post, predecessors in enumerate(generated.precedences)
            for pred in predecessors
        }
        edge_count_by_participant = [0] * source.n_business
        participants_by_meeting = [set() for _ in range(source.n_meetings)]
        for participant, meetings in enumerate(source.meetings_by_business):
            for meeting in meetings:
                participants_by_meeting[meeting].add(participant)
        for pred, post in edges:
            shared = participants_by_meeting[pred] & participants_by_meeting[post]
            if len(shared) != 1:
                raise ValueError(
                    f"Edge {pred + 1}->{post + 1} in {instance_name} "
                    "must belong to exactly one participant"
                )
            participant = next(iter(shared))
            edge_count_by_participant[participant] += 1
            if assignment[pred] >= assignment[post]:
                raise ValueError(
                    f"Witness-inconsistent edge in {instance_name}"
                )

        expected_by_participant = [
            math.floor(gamma * len(meetings) / 100)
            for meetings in source.meetings_by_business
        ]
        if edge_count_by_participant != expected_by_participant:
            raise ValueError(
                f"Per-participant edge budget mismatch in {instance_name}"
            )
        expected_edges = sum(expected_by_participant)
        if graph.direct_edge_count != expected_edges:
            raise ValueError(f"Direct edge count mismatch in {instance_name}")
        if int(row["target_direct_edges"]) != expected_edges:
            raise ValueError(f"Target edge count mismatch in {instance_name}")
        if int(row["actual_direct_edges"]) != graph.direct_edge_count:
            raise ValueError(f"Actual edge count mismatch in {instance_name}")
        if int(row["transitive_edges"]) != graph.transitive_edge_count:
            raise ValueError(f"Closure edge count mismatch in {instance_name}")
        if int(row["max_chain_distance"]) != graph.max_chain_distance:
            raise ValueError(f"Chain distance mismatch in {instance_name}")
        if row["construction"] != CONSTRUCTION_NAME:
            raise ValueError(f"Construction mismatch in {instance_name}")

        generated_by_source.setdefault(source_name, {})[gamma] = edges

    actual_files = {path.name for path in data_root.glob("*.dzn")}
    if actual_files != manifest_names:
        missing = sorted(manifest_names - actual_files)
        extra = sorted(actual_files - manifest_names)
        raise ValueError(
            f"Generated file set mismatch; missing={missing}, extra={extra}"
        )

    for source_name, by_gamma in generated_by_source.items():
        if set(by_gamma) != set(gammas):
            raise ValueError(f"Missing gamma level for {source_name}")
        for lower, higher in zip(gammas, gammas[1:]):
            if not by_gamma[lower] <= by_gamma[higher]:
                raise ValueError(
                    f"Non-nested edge sets for {source_name}: "
                    f"prec{lower} is not a subset of prec{higher}"
                )

    runner_by_name = {row["canonical_run_path"]: row for row in runner_rows}
    if set(runner_by_name) != manifest_names:
        raise ValueError("Runner manifest does not cover generated files")
    for name, row in runner_by_name.items():
        generated_path = data_root / name
        if row["sha256"] != file_sha256(generated_path):
            raise ValueError(f"Runner hash mismatch for {name}")
        if row["family"] != "precedence":
            raise ValueError(f"Runner family mismatch for {name}")
        if row["dataset_archive_url"] or row["dataset_archive_sha256"]:
            raise ValueError(
                f"Derived instance {name} must not claim official archive provenance"
            )

    return {
        "source_instances": len(sources),
        "gamma_levels": len(gammas),
        "generated_instances": len(generation_rows),
    }


def generate_dataset(
    source_directory: str | Path = DEFAULT_SOURCE_DIRECTORY,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    *,
    gammas: Iterable[int] = DEFAULT_GAMMAS,
    global_seed: int = DEFAULT_GLOBAL_SEED,
    sat_backend: str = "cadical153",
) -> dict[str, int]:
    source_root = Path(source_directory).resolve()
    output_root = Path(output_directory).resolve()
    gamma_levels = _validate_gamma_levels(int(gamma) for gamma in gammas)
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    if output_root.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing output directory {output_root}"
        )

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_root.name}-",
        dir=output_root.parent,
    ) as temporary:
        staging = Path(temporary)
        _generate_into_directory(
            source_root,
            staging,
            dataset_directory_name=output_root.name,
            gammas=gamma_levels,
            global_seed=global_seed,
            sat_backend=sat_backend,
        )
        summary = validate_dataset(staging, source_root)
        shutil.copytree(staging, output_root)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or validate reproducible, witness-preserving precedence "
            "density stress instances."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_SOURCE_DIRECTORY),
    )
    generate_parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIRECTORY),
    )
    generate_parser.add_argument(
        "--gammas",
        type=int,
        nargs="+",
        default=list(DEFAULT_GAMMAS),
    )
    generate_parser.add_argument(
        "--global-seed",
        type=int,
        default=DEFAULT_GLOBAL_SEED,
    )
    generate_parser.add_argument(
        "--sat-backend",
        default="cadical153",
        help="python-sat backend used only to construct canonical witnesses",
    )

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_SOURCE_DIRECTORY),
    )
    validate_parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_OUTPUT_DIRECTORY),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "generate":
        summary = generate_dataset(
            args.source_dir,
            args.output_dir,
            gammas=args.gammas,
            global_seed=args.global_seed,
            sat_backend=args.sat_backend,
        )
    else:
        summary = validate_dataset(args.data_dir, args.source_dir)
    print(
        "validated "
        f"{summary['generated_instances']} generated instances from "
        f"{summary['source_instances']} sources at "
        f"{summary['gamma_levels']} gamma levels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
