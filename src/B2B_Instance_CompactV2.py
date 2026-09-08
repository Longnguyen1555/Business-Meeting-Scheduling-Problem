from __future__ import annotations

from bisect import bisect_right
from typing import Literal

from pysat.card import CardEnc, EncType
from pysat.formula import CNF

from B2B_Instance import (
    B2BInstance,
    B2BModelArtifacts,
    B2BSATModel,
    B2BSolutionStats,
    ObjectiveTier,
)


CapacityMode = Literal[
    "meeting",
    "global_cluster",
    "slot_cluster",
    "busy",
    "slot_cluster_busy",
]
CapacityCardinality = Literal[
    "seqcounter",
    "cardnet",
    "totalizer",
    "adaptive",
]
BGCounterMode = Literal["repeated_seqcounter", "shared_dp"]
V2ObjectiveMode = Literal[
    "ir",
    "bg_d2",
    "ir_is",
    "bg_ir_is",
    "max_idle_is",
    "sla_idle",
]
V2PrecedenceEncoding = Literal["pairwise", "sparse_suffix", "hybrid_suffix"]

VALID_CAPACITY_MODES = {
    "meeting",
    "global_cluster",
    "slot_cluster",
    "busy",
    "slot_cluster_busy",
}
VALID_CAPACITY_CARDINALITY = {
    "seqcounter",
    "cardnet",
    "totalizer",
    "adaptive",
}
VALID_BG_COUNTER_MODES = {"repeated_seqcounter", "shared_dp"}
VALID_V2_OBJECTIVE_MODES = {
    "ir",
    "bg_d2",
    "ir_is",
    "bg_ir_is",
    "max_idle_is",
    "sla_idle",
}
VALID_V2_PRECEDENCE_ENCODINGS = {
    "pairwise",
    "sparse_suffix",
    "hybrid_suffix",
}


def compute_solution_stats_v2(
    inst: B2BInstance,
    assignment: list[int],
    *,
    participants: tuple[int, ...] | None = None,
    objective_mode: str = "ir",
    idle_sla_threshold: int = 2,
) -> B2BSolutionStats:
    """Independent schedule-side evaluator for Compact-v2 objectives.

    This deliberately recomputes metrics from the decoded assignment and does
    not inspect SAT auxiliary variables.
    """

    if objective_mode not in VALID_V2_OBJECTIVE_MODES:
        raise ValueError(f"Unknown objective_mode={objective_mode!r}")
    if idle_sla_threshold < 0:
        raise ValueError("idle_sla_threshold must be non-negative")

    selected_participants = (
        tuple(
            participant
            for participant, meetings in enumerate(inst.meetings_by_business)
            if len(meetings) >= 2
        )
        if participants is None
        else participants
    )

    meetings_per_slot: list[list[int]] = [
        [] for _ in range(inst.n_total_slots)
    ]
    for meeting, slot in enumerate(assignment):
        if 0 <= slot < inst.n_total_slots:
            meetings_per_slot[slot].append(meeting)

    participant_idle = [0] * inst.n_business
    participant_break_groups = [0] * inst.n_business
    for participant, meetings in enumerate(inst.meetings_by_business):
        slots = sorted(
            assignment[meeting]
            for meeting in meetings
            if 0 <= meeting < len(assignment) and assignment[meeting] >= 0
        )
        if len(slots) >= 2:
            participant_idle[participant] = (
                slots[-1] - slots[0] + 1 - len(slots)
            )
            participant_break_groups[participant] = sum(
                right > left + 1
                for left, right in zip(slots, slots[1:])
            )

    objective_values = [
        participant_idle[participant]
        for participant in selected_participants
    ]
    objective_gap = (
        max(objective_values) - min(objective_values)
        if len(objective_values) >= 2
        else 0
    )
    max_idle = max(objective_values, default=0)
    all_participant_gap = (
        max(participant_idle) - min(participant_idle)
        if len(participant_idle) >= 2
        else 0
    )
    break_group_range = (
        max(participant_break_groups) - min(participant_break_groups)
        if len(participant_break_groups) >= 2
        else 0
    )
    total_idle = sum(participant_idle)
    total_break_groups = sum(participant_break_groups)
    sla_violations = sum(
        participant_idle[p] > idle_sla_threshold
        for p in selected_participants
    )
    sla_excess = sum(
        max(0, participant_idle[p] - idle_sla_threshold)
        for p in selected_participants
    )

    objective_vectors = {
        "ir": (objective_gap,),
        "bg_d2": (total_break_groups,),
        "ir_is": (objective_gap, total_idle),
        "bg_ir_is": (total_break_groups, objective_gap, total_idle),
        "max_idle_is": (max_idle, total_idle),
        "sla_idle": (sla_violations, sla_excess, total_idle),
    }

    return B2BSolutionStats(
        total_breaks=total_idle,
        participant_breaks=participant_idle,
        objective_gap=objective_gap,
        all_participant_idle_range=all_participant_gap,
        objective_participants=selected_participants,
        meetings_per_slot=meetings_per_slot,
        busy_participants_per_slot=[
            2 * len(meetings) for meetings in meetings_per_slot
        ],
        participant_break_groups=participant_break_groups,
        total_break_groups=total_break_groups,
        break_group_range=break_group_range,
        objective_mode=objective_mode,
        objective_vector=objective_vectors[objective_mode],
    )


class B2BSATModelV2(B2BSATModel):
    """Compact-v2 extension of the latest repository B2B SAT/MaxSAT core.

    Preserved from the latest core:
      * exact parser semantics and validation;
      * reduced domains with unary filtering, precedence propagation,
        participant matching GAC, and saturated table-slot propagation;
      * exact used-slot channeling and first/last SpanThreshold idle encoding;
      * current pairwise and sparse-suffix precedence encodings;
      * current objective modes and ObjectiveTier interface.

    Compact-v2 additions:
      * shared O(r*K) exact unary break-group counter;
      * slot-aware participant-AMO clustering for table capacity;
      * independent capacity representation and cardinality encoding factors;
      * optional capacity on exact busy-participant variables;
      * optional sparse/dense hybrid suffix precedence;
      * lexicographic max-idle-then-total-idle objective;
      * lexicographic waiting-SLA violations/excess/total-idle objective.
    """

    def __init__(
        self,
        inst: B2BInstance,
        precedence_mode: str | None = None,
        encoding_variant: str = "imp12+",
        domain_mode: str = "reduced",
        *,
        precedence_encoding: str | None = None,
        precedence_graph: str | None = None,
        domain_filter_graph: str = "distance_closure",
        objective_mode: str = "ir",
        capacity_mode: str | None = None,
        capacity_cardinality: str = "seqcounter",
        bg_counter_mode: str = "shared_dp",
        idle_sla_threshold: int = 2,
        hybrid_suffix_density: float = 0.70,
    ) -> None:
        if objective_mode not in VALID_V2_OBJECTIVE_MODES:
            raise ValueError(f"Unknown objective_mode={objective_mode!r}")
        if precedence_encoding not in {None, *VALID_V2_PRECEDENCE_ENCODINGS}:
            raise ValueError(
                f"Unknown precedence_encoding={precedence_encoding!r}"
            )
        if capacity_cardinality not in VALID_CAPACITY_CARDINALITY:
            raise ValueError(
                f"Unknown capacity_cardinality={capacity_cardinality!r}"
            )
        if bg_counter_mode not in VALID_BG_COUNTER_MODES:
            raise ValueError(f"Unknown bg_counter_mode={bg_counter_mode!r}")
        if idle_sla_threshold < 0:
            raise ValueError("idle_sla_threshold must be non-negative")
        if not 0.0 <= hybrid_suffix_density <= 1.0:
            raise ValueError("hybrid_suffix_density must be in [0,1]")

        requested_precedence = precedence_encoding
        requested_objective = objective_mode

        if requested_precedence == "hybrid_suffix" and precedence_mode is not None:
            raise ValueError(
                "precedence_mode cannot be combined with hybrid_suffix; "
                "use precedence_graph explicitly"
            )

        # The base constructor validates against the latest repository enums.
        # Feed equivalent legacy values during construction, then restore the
        # Compact-v2 choices before any CNF is built.
        base_precedence = (
            "sparse_suffix"
            if requested_precedence == "hybrid_suffix"
            else requested_precedence
        )
        base_objective = (
            requested_objective
            if requested_objective in {"ir", "bg_d2", "ir_is", "bg_ir_is"}
            else "ir"
        )

        super().__init__(
            inst=inst,
            precedence_mode=precedence_mode,
            encoding_variant=encoding_variant,
            domain_mode=domain_mode,
            precedence_encoding=base_precedence,
            precedence_graph=precedence_graph,
            domain_filter_graph=domain_filter_graph,
            objective_mode=base_objective,
        )

        self.objective_mode = requested_objective
        if requested_precedence == "hybrid_suffix":
            self.precedence_encoding = "hybrid_suffix"
            self.precedence_mode = "factorial"
            self.precedence_configuration = (
                f"hybrid_suffix+{self.precedence_graph}"
            )

        if capacity_mode is None:
            capacity_mode = (
                "global_cluster"
                if encoding_variant == "imp12+"
                else "meeting"
            )
        if capacity_mode not in VALID_CAPACITY_MODES:
            raise ValueError(f"Unknown capacity_mode={capacity_mode!r}")

        self.capacity_mode = capacity_mode
        self.capacity_cardinality = capacity_cardinality
        self.bg_counter_mode = bg_counter_mode
        self.idle_sla_threshold = idle_sla_threshold
        self.hybrid_suffix_density = hybrid_suffix_density

        self._precedence_dense_suffix_meetings = 0
        self._precedence_dense_suffix_clauses = 0

    # ------------------------------------------------------------------
    # Capacity cardinality portfolio
    # ------------------------------------------------------------------

    @staticmethod
    def _adaptive_capacity_encoding(n_lits: int, bound: int) -> str:
        """Conservative deterministic portfolio rule for experimental use.

        This is intentionally a simple heuristic, not claimed to be globally
        optimal. Keep seqcounter as the default experimental control.
        """
        if n_lits <= 16 or bound <= 4:
            return "seqcounter"
        if bound * 4 <= n_lits:
            return "totalizer"
        return "cardnet"

    def _add_capacity_atmost(
        self,
        cnf: CNF,
        lits: list[int],
        bound: int,
    ) -> None:
        if bound < 0:
            cnf.append([])
            return
        if bound == 0:
            cnf.extend([[-lit] for lit in lits])
            return
        if bound >= len(lits):
            return

        mode = self.capacity_cardinality
        if mode == "adaptive":
            mode = self._adaptive_capacity_encoding(len(lits), bound)

        enc_type = {
            "seqcounter": EncType.seqcounter,
            "cardnet": EncType.cardnetwrk,
            "totalizer": EncType.totalizer,
        }[mode]
        encoding = CardEnc.atmost(
            lits=lits,
            bound=bound,
            vpool=self.vpool,
            encoding=enc_type,
        )
        cnf.extend(encoding.clauses)

    def _add_capacity_over_meetings(self, cnf: CNF) -> None:
        self.enabled_constraints.append(
            f"table capacity over schedule variables; "
            f"cardinality={self.capacity_cardinality}"
        )
        for slot in range(self.inst.n_total_slots):
            lits = [
                lit
                for meeting in range(self.inst.n_meetings)
                if (lit := self.x_or_none(meeting, slot)) is not None
            ]
            self._add_capacity_atmost(cnf, lits, self.inst.n_tables)

    def _add_cluster_capacity(self, cnf: CNF) -> None:
        self.enabled_constraints.append(
            "global participant-AMO clustered table capacity with forward "
            f"channeling; cardinality={self.capacity_cardinality}"
        )
        clusters = self._compute_meeting_clusters()
        for slot in range(self.inst.n_total_slots):
            active_clusters: list[int] = []
            for cluster, meetings in enumerate(clusters):
                member_lits = [
                    lit
                    for meeting in meetings
                    if (lit := self.x_or_none(meeting, slot)) is not None
                ]
                if not member_lits:
                    continue
                if len(member_lits) == 1:
                    active_clusters.append(member_lits[0])
                    continue

                cluster_lit = self.cluster_active(cluster, slot)
                active_clusters.append(cluster_lit)
                for lit in member_lits:
                    cnf.append([-lit, cluster_lit])

            self._add_capacity_atmost(
                cnf,
                active_clusters,
                self.inst.n_tables,
            )

    def slot_cluster_active(self, slot: int, cluster: int) -> int:
        return self.vpool.id(("slotClusterActive", slot, cluster))

    def _compute_slot_clusters(self, slot: int) -> list[list[int]]:
        """Greedy disjoint AMO cover over meetings active at one slot."""
        remaining = {
            meeting
            for meeting in range(self.inst.n_meetings)
            if self.x_or_none(meeting, slot) is not None
        }
        clusters: list[list[int]] = []

        while remaining:
            best: list[int] = []
            for meetings in self.inst.meetings_by_business:
                candidate = sorted(remaining.intersection(meetings))
                if len(candidate) > len(best):
                    best = candidate
            if not best:
                best = [min(remaining)]
            clusters.append(best)
            remaining.difference_update(best)

        return clusters

    def _add_slot_cluster_capacity(self, cnf: CNF) -> None:
        self.enabled_constraints.append(
            "slot-aware participant-AMO clustered table capacity with forward "
            f"channeling; cardinality={self.capacity_cardinality}"
        )
        for slot in range(self.inst.n_total_slots):
            active_clusters: list[int] = []
            for cluster_index, meetings in enumerate(
                self._compute_slot_clusters(slot)
            ):
                member_lits = [
                    lit
                    for meeting in meetings
                    if (lit := self.x_or_none(meeting, slot)) is not None
                ]
                if not member_lits:
                    continue
                if len(member_lits) == 1:
                    active_clusters.append(member_lits[0])
                    continue

                cluster_lit = self.slot_cluster_active(slot, cluster_index)
                active_clusters.append(cluster_lit)
                for lit in member_lits:
                    cnf.append([-lit, cluster_lit])

            self._add_capacity_atmost(
                cnf,
                active_clusters,
                self.inst.n_tables,
            )

    # ------------------------------------------------------------------
    # Exact used-slot channeling separated from implied constraints
    # ------------------------------------------------------------------

    def _channel_used_slots(self, cnf: CNF) -> None:
        if self._used_slots_channeled:
            return
        self._used_slots_channeled = True
        self.enabled_constraints.append("exact schedule <-> usedSlot channeling")

        for participant, meetings in enumerate(self.inst.meetings_by_business):
            for slot in range(self.inst.n_total_slots):
                used = self.used_or_none(participant, slot)
                if used is None:
                    continue
                scheduled = [
                    lit
                    for meeting in meetings
                    if (lit := self.x_or_none(meeting, slot)) is not None
                ]
                for lit in scheduled:
                    cnf.append([-lit, used])
                cnf.append([-used] + scheduled)

    def _busy_literals(self, slot: int) -> list[int]:
        return [
            used
            for participant in range(self.inst.n_business)
            if (used := self.used_or_none(participant, slot)) is not None
        ]

    def _add_busy_capacity(self, cnf: CNF) -> None:
        self._channel_used_slots(cnf)
        self.enabled_constraints.append(
            "exact busy-participant table capacity sum(y[p,t]) <= 2*L; "
            f"cardinality={self.capacity_cardinality}"
        )
        bound = 2 * self.inst.n_tables
        for slot in range(self.inst.n_total_slots):
            self._add_capacity_atmost(
                cnf,
                self._busy_literals(slot),
                bound,
            )

    def _add_even_busy_parity_only(self, cnf: CNF) -> None:
        marker = "exact even number of busy participants"
        if marker not in self.enabled_constraints:
            self.enabled_constraints.append(marker)

        for slot in range(self.inst.n_total_slots):
            lits = self._busy_literals(slot)
            if not lits:
                continue
            if len(lits) == 1:
                cnf.append([-lits[0]])
                continue

            parity = lits[0]
            for index, lit in enumerate(lits[1:], start=1):
                next_parity = self.vpool.id(
                    ("busyParityV2", tuple(lits), index)
                )
                cnf.append([parity, lit, -next_parity])
                cnf.append([-parity, -lit, -next_parity])
                cnf.append([parity, -lit, next_parity])
                cnf.append([-parity, lit, next_parity])
                parity = next_parity
            cnf.append([-parity])

    def _add_implied_package(self, cnf: CNF) -> None:
        if not (self.use_implied_1 or self.use_implied_2):
            return

        self._channel_used_slots(cnf)

        if self.use_implied_1:
            for participant in range(self.inst.n_business):
                self._add_implied_constraint_1(cnf, participant)

        if self.use_implied_2:
            busy_is_primary_capacity = self.capacity_mode in {
                "busy",
                "slot_cluster_busy",
            }
            if busy_is_primary_capacity:
                # The at-most 2L part is already present exactly once.
                if self.use_further_improvements:
                    self._add_even_busy_parity_only(cnf)
            else:
                # Preserve the latest repository IC2/IC2+ encoding exactly.
                self._add_implied_constraint_2(cnf)

    def _add_selected_capacity(self, cnf: CNF) -> None:
        if self.capacity_mode == "meeting":
            self._add_capacity_over_meetings(cnf)
        elif self.capacity_mode == "global_cluster":
            self._add_cluster_capacity(cnf)
        elif self.capacity_mode == "slot_cluster":
            self._add_slot_cluster_capacity(cnf)
        elif self.capacity_mode == "busy":
            self._add_busy_capacity(cnf)
        elif self.capacity_mode == "slot_cluster_busy":
            self._add_slot_cluster_capacity(cnf)
            self._add_busy_capacity(cnf)
        else:  # defensive guard
            raise AssertionError(self.capacity_mode)

    # ------------------------------------------------------------------
    # Shared exact break-group threshold counter
    # ------------------------------------------------------------------

    def _add_exact_cardinality_thresholds(
        self,
        cnf: CNF,
        literals: list[int],
        *,
        participant: int,
        family: str,
        upper_bound: int,
    ) -> list[int]:
        if self.bg_counter_mode == "repeated_seqcounter":
            return super()._add_exact_cardinality_thresholds(
                cnf,
                literals,
                participant=participant,
                family=family,
                upper_bound=upper_bound,
            )

        if family != "break_groups":
            raise ValueError(f"Unsupported threshold family={family!r}")

        n = len(literals)
        K = min(upper_bound, n)
        if K == 0:
            return []

        # S[i,k] <-> at least k of the first i inputs are true.
        previous: dict[int, int] = {}
        for i, x in enumerate(literals, start=1):
            current: dict[int, int] = {}
            for k in range(1, min(i, K) + 1):
                out = (
                    self.break_group_threshold(participant, k)
                    if i == n
                    else self.vpool.id(
                        ("breakGroupSharedCounter", participant, i, k)
                    )
                )
                current[k] = out

                if k == 1:
                    if i == 1:
                        self._add_equiv(cnf, out, x)
                    else:
                        self._add_equiv_or(cnf, out, previous[1], x)
                    continue

                if k not in previous:
                    # S[i,k] <-> S[i-1,k-1] AND x.
                    lower = previous[k - 1]
                    cnf.append([-out, lower])
                    cnf.append([-out, x])
                    cnf.append([-lower, -x, out])
                    continue

                same = previous[k]
                lower = previous[k - 1]
                # out <-> same OR (lower AND x)
                cnf.append([-same, out])
                cnf.append([-lower, -x, out])
                cnf.append([-out, same, lower])
                cnf.append([-out, same, x])

            previous = current

        thresholds = [previous[k] for k in range(1, K + 1)]
        for index in range(1, len(thresholds)):
            cnf.append([-thresholds[index], thresholds[index - 1]])
        return thresholds

    # ------------------------------------------------------------------
    # Optional hybrid sparse/dense suffix precedence
    # ------------------------------------------------------------------

    def _build_dense_precedence_suffixes(
        self,
        cnf: CNF,
        meeting: int,
    ) -> dict[int, int]:
        slots = self._eligible_slots[meeting]
        if len(slots) <= 1:
            return {}

        suffixes: dict[int, int] = {}
        next_lit = self.x(meeting, slots[-1])
        suffixes[len(slots) - 1] = next_lit

        # split==0 is handled by a unit clause in the precedence linker, so
        # only cuts 1..n-1 need suffix literals.
        for cut in range(len(slots) - 2, 0, -1):
            here = self.vpool.id(("precedenceDenseSuffix", meeting, cut))
            current = self.x(meeting, slots[cut])
            self._add_equiv_or(cnf, here, current, next_lit)
            self._precedence_dense_suffix_clauses += 3
            suffixes[cut] = here
            next_lit = here

        self._precedence_dense_suffix_meetings += 1
        return suffixes

    def _add_hybrid_suffix_precedences(self, cnf: CNF) -> None:
        self.enabled_constraints.append(
            "hybrid precedence suffixes: sparse cuts unless requested-cut "
            f"density >= {self.hybrid_suffix_density:.2f}"
        )

        links: list[tuple[int, int, int]] = []
        cuts_by_pred: dict[int, set[int]] = {}

        for post, distances in enumerate(self._precedence_distances):
            post_slots = self._eligible_slots[post]
            for pred, distance in sorted(distances.items()):
                pred_slots = self._eligible_slots[pred]
                if not pred_slots or not post_slots:
                    continue
                for post_slot in post_slots:
                    split = bisect_right(pred_slots, post_slot - distance)
                    post_lit = self.x(post, post_slot)
                    if split == 0:
                        cnf.append([-post_lit])
                        self._precedence_sparse_link_clauses += 1
                    elif split < len(pred_slots):
                        cuts_by_pred.setdefault(pred, set()).add(split)
                        links.append((post_lit, pred, split))

        self._precedence_unique_suffix_cuts = sum(
            len(cuts) for cuts in cuts_by_pred.values()
        )
        suffix_maps: dict[int, dict[int, int]] = {}
        for pred, cuts in cuts_by_pred.items():
            domain_size = len(self._eligible_slots[pred])
            density = len(cuts) / max(1, domain_size - 1)
            if density >= self.hybrid_suffix_density:
                suffix_maps[pred] = self._build_dense_precedence_suffixes(
                    cnf, pred
                )
            else:
                suffix_maps[pred] = self._build_sparse_precedence_suffixes(
                    cnf, pred, cuts
                )

        for post_lit, pred, split in links:
            cnf.append([-post_lit, -suffix_maps[pred][split]])
            self._precedence_sparse_link_clauses += 1

    def _add_precedences(self, cnf: CNF) -> None:
        if self.precedence_encoding == "hybrid_suffix":
            self._add_hybrid_suffix_precedences(cnf)
            return
        super()._add_precedences(cnf)

    # ------------------------------------------------------------------
    # New exact objective families
    # ------------------------------------------------------------------

    def _add_exact_max(
        self,
        cnf: CNF,
        thresholds_by_participant: list[list[int]],
        *,
        participants: tuple[int, ...],
        family: str,
    ) -> list[int]:
        """Return unary literals whose true count equals max participant value."""
        global_upper = max(
            (
                len(thresholds_by_participant[p])
                for p in participants
            ),
            default=0,
        )
        max_lits: list[int] = []
        for amount in range(1, global_upper + 1):
            out = self.range_max(family, amount)
            present = [
                thresholds_by_participant[p][amount - 1]
                for p in participants
                if amount <= len(thresholds_by_participant[p])
            ]
            if not present:
                cnf.append([-out])
            else:
                for lit in present:
                    cnf.append([-lit, out])
                cnf.append([-out] + present)
            max_lits.append(out)

        for index in range(1, len(max_lits)):
            cnf.append([-max_lits[index], max_lits[index - 1]])
        return max_lits

    def _build_objective_family(self, cnf: CNF):
        if self.objective_mode in {"ir", "bg_d2", "ir_is", "bg_ir_is"}:
            return super()._build_objective_family(cnf)

        idle_thresholds = self._add_span_break_thresholds(cnf)
        idle_sum_lits = [
            literal
            for participant in self.objective_participants
            for literal in idle_thresholds[participant]
        ]

        group_ends = [[] for _ in range(self.inst.n_business)]
        group_thresholds = [[] for _ in range(self.inst.n_business)]
        group_range_lits: list[int] = []
        idle_range_lits: list[int] = []

        if self.objective_mode == "max_idle_is":
            max_idle_lits = self._add_exact_max(
                cnf,
                idle_thresholds,
                participants=self.objective_participants,
                family="idle_slots",
            )
            idle_weight = len(idle_sum_lits) + 1
            tiers = (
                ObjectiveTier(
                    "max_internal_idle_slots",
                    tuple(max_idle_lits),
                    len(max_idle_lits),
                    idle_weight,
                ),
                ObjectiveTier(
                    "total_internal_idle_slots",
                    tuple(idle_sum_lits),
                    len(idle_sum_lits),
                    1,
                ),
            )
            name = "lexicographic_max_idle_then_idle_sum"

        elif self.objective_mode == "sla_idle":
            tau = self.idle_sla_threshold
            violation_lits: list[int] = []
            excess_lits: list[int] = []
            for participant in self.objective_participants:
                thresholds = idle_thresholds[participant]
                if len(thresholds) > tau:
                    violation_lits.append(thresholds[tau])
                excess_lits.extend(thresholds[tau:])

            idle_upper = len(idle_sum_lits)
            excess_upper = len(excess_lits)
            excess_weight = idle_upper + 1
            violation_weight = (excess_upper + 1) * excess_weight

            tiers = (
                ObjectiveTier(
                    "participants_above_idle_sla",
                    tuple(violation_lits),
                    len(violation_lits),
                    violation_weight,
                ),
                ObjectiveTier(
                    "idle_slots_above_sla",
                    tuple(excess_lits),
                    excess_upper,
                    excess_weight,
                ),
                ObjectiveTier(
                    "total_internal_idle_slots",
                    tuple(idle_sum_lits),
                    idle_upper,
                    1,
                ),
            )
            name = (
                f"lexicographic_idle_sla_{tau}_"
                "violations_excess_total_idle"
            )
        else:
            raise AssertionError(self.objective_mode)

        return (
            idle_thresholds,
            idle_range_lits,
            group_ends,
            group_thresholds,
            group_range_lits,
            tiers,
            name,
        )

    # ------------------------------------------------------------------
    # Compact-v2 build entry point
    # ------------------------------------------------------------------

    def build_base_cnf(self) -> B2BModelArtifacts:
        if self._artifacts is not None:
            return self._artifacts

        cnf = CNF()
        filter_label = (
            "direct E"
            if self.domain_filter_graph == "direct"
            else "distance-labelled E*"
        )
        self.enabled_constraints = [
            f"Compact-v2 objective family: {self.objective_mode}",
            f"F-selected {filter_label} domain propagation and cycle detection",
            "precedence configuration: "
            f"F={self.domain_filter_graph}, "
            f"P={self.precedence_encoding}, G={self.precedence_graph}",
            f"capacity_mode={self.capacity_mode}",
            f"capacity_cardinality={self.capacity_cardinality}",
            f"bg_counter_mode={self.bg_counter_mode}",
        ]
        if self.domain_mode == "full":
            self.enabled_constraints.append(
                "Full Domain MxT schedule variables + explicit unary exclusions"
            )
        else:
            self.enabled_constraints.append(
                "Reduced Domain variables after unary filtering + matching GAC + "
                "slot saturation"
            )

        if self.graph.cycle_nodes:
            cnf.append([])
            self.enabled_constraints.append("strict precedence cycle -> UNSAT")

        self._add_assignment(cnf)
        self._add_participant_collision(cnf)
        self._add_selected_capacity(cnf)
        self._add_precedences(cnf)
        self._add_implied_package(cnf)

        (
            idle_threshold_lits,
            idle_range_lits,
            break_group_end_lits,
            break_group_threshold_lits,
            break_group_range_lits,
            objective_tiers,
            objective_name,
        ) = self._build_objective_family(cnf)

        n_vars = max(self.vpool.top, cnf.nv)
        n_primary_variables = len(self._schedule_vars)
        clause_lengths = [len(clause) for clause in cnf.clauses]

        objective_encoding = {
            "ir": "linear first/last span with exact unary thresholds",
            "ir_is": "exact_idle_span_threshold_range_then_idle_sum",
            "bg_d2": (
                "exact_break_group_threshold_range_cap_d2_"
                f"counter-{self.bg_counter_mode}"
            ),
            "bg_ir_is": (
                "exact_break_group_sum_then_idle_range_then_idle_sum_"
                f"counter-{self.bg_counter_mode}"
            ),
            "max_idle_is": "exact_max_idle_threshold_then_idle_sum",
            "sla_idle": (
                f"idle_sla_{self.idle_sla_threshold}_"
                "violations_excess_total_idle"
            ),
        }[self.objective_mode]

        self._artifacts = B2BModelArtifacts(
            cnf=cnf,
            objective_lits=list(objective_tiers[0].literals),
            objective_name=objective_name,
            objective_mode=self.objective_mode,
            objective_tiers=objective_tiers,
            objective_participants=self.objective_participants,
            objective_gap_lits=idle_range_lits,
            idle_threshold_lits_by_participant=idle_threshold_lits,
            break_group_end_lits_by_participant=break_group_end_lits,
            break_group_threshold_lits_by_participant=break_group_threshold_lits,
            break_group_range_lits=break_group_range_lits,
            hole_lits_by_participant=[[] for _ in range(self.inst.n_business)],
            sorted_hole_lits_by_participant=idle_threshold_lits,
            n_vars=n_vars,
            n_clauses=len(cnf.clauses),
            n_primary_variables=n_primary_variables,
            n_auxiliary_variables=n_vars - n_primary_variables,
            n_hard_literals=sum(clause_lengths),
            max_hard_clause_length=max(clause_lengths, default=0),
            n_unit_hard_clauses=sum(length == 1 for length in clause_lengths),
            n_binary_hard_clauses=sum(length == 2 for length in clause_lengths),
            n_ternary_hard_clauses=sum(length == 3 for length in clause_lengths),
            n_long_hard_clauses=sum(length >= 4 for length in clause_lengths),
            encoding_variant=self.encoding_variant,
            precedence_mode=self.precedence_mode,
            precedence_encoding=self.precedence_encoding,
            precedence_graph=self.precedence_graph,
            precedence_configuration=self.precedence_configuration,
            domain_mode=self.domain_mode,
            domain_filter_graph=self.domain_filter_graph,
            domain_filter_iterations=self.domain_filter_iterations,
            domain_filter_seconds=self.domain_filter_seconds,
            enabled_constraints=list(self.enabled_constraints),
            full_schedule_candidates=self.full_schedule_candidates,
            unary_eligible_schedule_candidates=self.unary_eligible_schedule_candidates,
            initial_schedule_candidates=self.initial_schedule_candidates,
            reduced_schedule_candidates=self.reduced_schedule_candidates,
            active_schedule_candidates=self.active_schedule_candidates,
            unary_removed_schedule_candidates=(
                self.full_schedule_candidates
                - self.unary_eligible_schedule_candidates
            ),
            preprocessing_removed_schedule_candidates=(
                self.unary_eligible_schedule_candidates
                - self.reduced_schedule_candidates
            ),
            removed_schedule_candidates=(
                self.initial_schedule_candidates
                - self.reduced_schedule_candidates
            ),
            precedence_direct_edges=self.graph.direct_edge_count,
            precedence_transitive_edges=self.graph.transitive_edge_count,
            precedence_cycle_nodes=self.graph.cycle_nodes,
            precedence_max_distance=self.graph.max_chain_distance,
            precedence_relation_edges=sum(
                len(distances) for distances in self._precedence_distances
            ),
            precedence_pairwise_clauses=self._precedence_pairwise_clauses,
            precedence_sparse_link_clauses=self._precedence_sparse_link_clauses,
            precedence_unique_suffix_cuts=self._precedence_unique_suffix_cuts,
            objective_encoding=objective_encoding,
        )
        return self._artifacts

    def compute_stats(self, assignment: list[int]) -> B2BSolutionStats:
        return compute_solution_stats_v2(
            self.inst,
            assignment,
            participants=self.objective_participants,
            objective_mode=self.objective_mode,
            idle_sla_threshold=self.idle_sla_threshold,
        )

    def compact_v2_metadata(self) -> dict[str, object]:
        return {
            "capacity_mode": self.capacity_mode,
            "capacity_cardinality": self.capacity_cardinality,
            "bg_counter_mode": self.bg_counter_mode,
            "idle_sla_threshold": self.idle_sla_threshold,
            "hybrid_suffix_density": self.hybrid_suffix_density,
            "precedence_dense_suffix_meetings": (
                self._precedence_dense_suffix_meetings
            ),
            "precedence_dense_suffix_clauses": (
                self._precedence_dense_suffix_clauses
            ),
        }
