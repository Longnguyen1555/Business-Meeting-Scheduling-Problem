from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from B2B_Instance import B2BInstance, VALID_OBJECTIVE_MODES


@dataclass(frozen=True)
class JournalScheduleMetrics:
    """Objective metrics computed without reading encoder literals."""

    participant_internal_idle_slots: tuple[int, ...]
    total_internal_idle_slots: int
    idle_range_pstar: int
    participant_break_groups: tuple[int, ...]
    total_break_groups: int
    break_group_range: int
    objective_mode: str
    objective_vector: tuple[int, ...]
    historical_fairness_cap_satisfied: bool


def _occupied_slots(
    instance: B2BInstance,
    assignment: Iterable[int],
) -> list[list[int]]:
    values = list(assignment)
    if len(values) != instance.n_meetings:
        raise ValueError(
            "assignment length does not match n_meetings: "
            f"{len(values)}!={instance.n_meetings}"
        )
    occupied: list[list[int]] = []
    for meetings in instance.meetings_by_business:
        slots = sorted(values[meeting] for meeting in meetings)
        if any(slot < 0 or slot >= instance.n_total_slots for slot in slots):
            raise ValueError("assignment contains an out-of-range slot")
        occupied.append(slots)
    return occupied


def evaluate_journal_schedule(
    instance: B2BInstance,
    assignment: Iterable[int],
    *,
    objective_mode: str,
) -> JournalScheduleMetrics:
    """Evaluate all journal metrics directly from meeting-slot assignments.

    This module deliberately does not import or call SAT objective helpers. It
    is therefore suitable as the independent side of correctness gates.
    Feasibility remains a separate check against the original hard constraints.
    """

    if objective_mode not in VALID_OBJECTIVE_MODES:
        raise ValueError(f"Unknown objective_mode={objective_mode!r}")

    occupied = _occupied_slots(instance, assignment)
    participant_idle = tuple(
        slots[-1] - slots[0] + 1 - len(slots) if len(slots) >= 2 else 0
        for slots in occupied
    )
    participant_groups = tuple(
        sum(right > left + 1 for left, right in zip(slots, slots[1:]))
        if len(slots) >= 2
        else 0
        for slots in occupied
    )

    pstar_values = tuple(
        participant_idle[participant]
        for participant, meetings in enumerate(instance.meetings_by_business)
        if len(meetings) >= 2
    )
    idle_range = (
        max(pstar_values) - min(pstar_values)
        if len(pstar_values) >= 2
        else 0
    )
    group_range = (
        max(participant_groups) - min(participant_groups)
        if len(participant_groups) >= 2
        else 0
    )
    idle_sum = sum(participant_idle)
    group_sum = sum(participant_groups)
    vectors = {
        "ir": (idle_range,),
        "bg_d2": (group_sum,),
        "ir_is": (idle_range, idle_sum),
        "bg_ir_is": (group_sum, idle_range, idle_sum),
    }
    return JournalScheduleMetrics(
        participant_internal_idle_slots=participant_idle,
        total_internal_idle_slots=idle_sum,
        idle_range_pstar=idle_range,
        participant_break_groups=participant_groups,
        total_break_groups=group_sum,
        break_group_range=group_range,
        objective_mode=objective_mode,
        objective_vector=vectors[objective_mode],
        historical_fairness_cap_satisfied=group_range <= 2,
    )


def objective_metric_errors(
    instance: B2BInstance,
    assignment: Iterable[int],
    *,
    objective_mode: str,
    encoded_vector: tuple[int, ...],
) -> list[str]:
    metrics = evaluate_journal_schedule(
        instance,
        assignment,
        objective_mode=objective_mode,
    )
    errors: list[str] = []
    if encoded_vector != metrics.objective_vector:
        errors.append(
            "independent objective mismatch: "
            f"encoded={encoded_vector}, evaluated={metrics.objective_vector}"
        )
    if objective_mode == "bg_d2" and not metrics.historical_fairness_cap_satisfied:
        errors.append(
            "independent historical fairness-cap violation: "
            f"Delta_G={metrics.break_group_range}>2"
        )
    return errors
