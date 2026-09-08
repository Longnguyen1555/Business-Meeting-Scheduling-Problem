from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pysat.card import CardEnc, EncType
from pysat.formula import CNF, IDPool, WCNF

PrecedenceMode = Literal["traditional", "staircase"]
PrecedenceEdgeMode = Literal["direct", "source-closure"]
EncodingVariant = Literal["basic", "imp1", "imp2", "imp12", "imp12+"]
ObjectiveMode = Literal["idle-range", "lexicographic", "lex-idlesum"]

VALID_PRECEDENCE_MODES = {"traditional", "staircase"}
VALID_PRECEDENCE_EDGE_MODES = {"direct", "source-closure"}
VALID_ENCODING_VARIANTS = {"basic", "imp1", "imp2", "imp12", "imp12+"}
# "lexicographic"  -> (IdleRange, IdleMax, IdleSum)
# "lex-idlesum"    -> (IdleRange, IdleSum), i.e. no IdleMax level
VALID_OBJECTIVE_MODES = {"idle-range", "lexicographic", "lex-idlesum"}
LEXICOGRAPHIC_MODES = {"lexicographic", "lex-idlesum"}


@dataclass(frozen=True)
class B2BInstance:
    """B2B instance using zero-based participants, meetings, and slots."""

    n_business: int
    n_meetings: int
    n_tables: int
    n_total_slots: int
    n_morning_slots: int
    requested: list[tuple[int, int, int]]  # (p1, p2, session), session in {1,2,3}
    meetings_by_business: list[list[int]]
    n_meetings_business: list[int]
    forbidden: list[set[int]]
    fixed: list[int | None]
    precedences: list[set[int]]  # precedences[post] = direct predecessors
    instance_name: str

    @property
    def morning_slots(self) -> range:
        return range(self.n_morning_slots)

    @property
    def afternoon_slots(self) -> range:
        return range(self.n_morning_slots, self.n_total_slots)

    @property
    def max_breaks_per_participant(self) -> int:
        """Legacy upper bound for the number of break groups from the paper."""
        return max(0, (self.n_total_slots - 1) // 2)

    @property
    def max_break_slots_per_participant(self) -> int:
        """Maximum possible idle slots strictly between first and last meetings."""
        return max(0, self.n_total_slots - 2)


@dataclass(frozen=True)
class PrecedenceGraphInfo:
    direct_predecessors: list[set[int]]
    transitive_predecessors: list[set[int]]
    source_augmented_predecessors: list[set[int]]
    successors: list[set[int]]
    source_nodes: tuple[int, ...]
    cycle_nodes: tuple[int, ...]
    direct_edge_count: int
    transitive_edge_count: int
    source_added_edge_count: int
    source_augmented_edge_count: int


@dataclass(frozen=True)
class B2BSolutionStats:
    """Schedule statistics for the internal-idle-slot range objective.

    ``participant_breaks[p]`` is retained for backward compatibility, but now
    stores the total number of idle time slots between consecutive meetings of
    participant p, not the number of contiguous break groups. ``fairness_gap``
    is evaluated only over participants with at least two meetings.
    """

    total_breaks: int
    participant_breaks: list[int]
    fairness_gap: int
    all_participant_idle_range: int
    objective_participants: tuple[int, ...]
    meetings_per_slot: list[list[int]]
    busy_participants_per_slot: list[int]

    @property
    def total_internal_idle_slots(self) -> int:
        """Sum of internal idle slots over all participants."""
        return self.total_breaks

    @property
    def participant_internal_idle_slots(self) -> list[int]:
        """Internal idle-slot count B(p) for every participant."""
        return self.participant_breaks

    @property
    def max_internal_idle_slots(self) -> int:
        """Bottleneck internal idle count max_p B(p).

        Participants outside P* have B(p) = 0, so maximizing over every
        participant agrees with maximizing over P*.
        """
        return max(self.participant_breaks, default=0)

    @property
    def idle_range(self) -> int:
        """Range of internal idle totals over the objective participant set."""
        return self.fairness_gap

    @property
    def objective_participant_ids(self) -> tuple[int, ...]:
        """One-based participant IDs used by the objective."""
        return tuple(p + 1 for p in self.objective_participants)

    # Backward-compatible names used by the existing benchmark runner.
    @property
    def total_break_slots(self) -> int:
        return self.total_internal_idle_slots

    @property
    def participant_break_slots(self) -> list[int]:
        return self.participant_internal_idle_slots


@dataclass(frozen=True)
class B2BModelArtifacts:
    cnf: CNF
    objective_lits: list[int]
    secondary_objective_lits: list[int]
    objective_name: str
    objective_mode: str
    objective_participants: tuple[int, ...]
    fairness_limit: int | None
    fairness_gap_lits: list[int]
    hole_lits_by_participant: list[list[int]]
    sorted_hole_lits_by_participant: list[list[int]]
    n_vars: int
    n_clauses: int
    encoding_variant: str
    precedence_mode: str
    precedence_edge_mode: str
    enabled_constraints: list[str]
    initial_schedule_candidates: int
    reduced_schedule_candidates: int
    removed_schedule_candidates: int
    precedence_direct_edges: int
    precedence_transitive_edges: int
    precedence_source_added_edges: int
    precedence_encoded_edges: int
    precedence_cycle_nodes: tuple[int, ...]


# ---------------------------------------------------------------------------
# MiniZinc .dzn parser
# ---------------------------------------------------------------------------


def _remove_comments(text: str) -> str:
    return "\n".join(
        line
        for raw in text.splitlines()
        if (line := raw.split("%", 1)[0].strip())
    )


def _extract_int(text: str, name: str) -> int:
    match = re.search(rf"\b{name}\s*=\s*(-?\d+)\s*;", text)
    if not match:
        raise ValueError(f"Cannot find integer field {name!r}")
    return int(match.group(1))


def _extract_block(text: str, name: str, open_char: str, close_char: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*{re.escape(open_char)}", text)
    if not match:
        raise ValueError(f"Cannot find block field {name!r}")

    start = match.end()
    depth = 1
    i = start
    while i < len(text):
        ch = text[i]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    raise ValueError(f"Cannot find end of block field {name!r}")


def _extract_int_list(block: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", block)]


def _extract_set_array(block: str) -> list[set[int]]:
    result: list[set[int]] = []
    i = 0
    while i < len(block):
        if block[i] != "{":
            i += 1
            continue
        start = i + 1
        depth = 1
        i += 1
        while i < len(block) and depth:
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    result.append({int(x) for x in re.findall(r"-?\d+", block[start:i])})
                    break
            i += 1
        i += 1
    return result


def read_instance(path: str | Path, *, validate_meetingsx_business: bool = True) -> B2BInstance:
    """Read one original B2B .dzn file and convert all indices to zero-based."""

    path = Path(path)
    text = _remove_comments(path.read_text(encoding="utf-8"))

    n_business = _extract_int(text, "nBusiness")
    n_meetings = _extract_int(text, "nMeetings")
    n_tables = _extract_int(text, "nTables")
    n_total_slots = _extract_int(text, "nTotalSlots")
    n_morning_slots = _extract_int(text, "nMorningSlots")

    if n_business < 0 or n_meetings < 0 or n_tables < 0 or n_total_slots < 0:
        raise ValueError("Instance sizes must be non-negative")
    if not 0 <= n_morning_slots <= n_total_slots:
        raise ValueError("nMorningSlots must be between 0 and nTotalSlots")

    requested_nums = _extract_int_list(_extract_block(text, "requested", "[", "]"))
    if len(requested_nums) != 3 * n_meetings:
        raise ValueError(
            f"requested must contain 3*nMeetings values; got {len(requested_nums)} "
            f"for nMeetings={n_meetings}"
        )

    requested: list[tuple[int, int, int]] = []
    for i in range(0, len(requested_nums), 3):
        p1 = requested_nums[i] - 1
        p2 = requested_nums[i + 1] - 1
        session = requested_nums[i + 2]
        m = i // 3
        if not (0 <= p1 < n_business and 0 <= p2 < n_business):
            raise ValueError(f"Meeting {m + 1} contains an invalid participant")
        if p1 == p2:
            raise ValueError(f"Meeting {m + 1} contains the same participant twice")
        if session not in {1, 2, 3}:
            raise ValueError(f"Meeting {m + 1} has invalid session code {session}")
        requested.append((p1, p2, session))

    meetingsx_business_raw = _extract_set_array(
        _extract_block(text, "meetingsxBusiness", "[", "]")
    )
    n_meetings_business = _extract_int_list(
        _extract_block(text, "nMeetingsBusiness", "[", "]")
    )
    forbidden_raw = _extract_set_array(_extract_block(text, "forbidden", "[", "]"))
    fixed_raw = _extract_int_list(_extract_block(text, "fixed", "[", "]"))
    precedences_raw = _extract_set_array(_extract_block(text, "precedences", "[", "]"))

    if len(n_meetings_business) != n_business:
        raise ValueError("nMeetingsBusiness must have nBusiness integers")
    if len(forbidden_raw) != n_business:
        raise ValueError("forbidden must have nBusiness sets")
    if len(fixed_raw) != n_meetings:
        raise ValueError("fixed must have nMeetings integers")
    if len(precedences_raw) != n_meetings:
        raise ValueError("precedences must have nMeetings sets")

    forbidden: list[set[int]] = []
    for p, slots in enumerate(forbidden_raw):
        converted = {slot - 1 for slot in slots if slot > 0}
        invalid = [slot + 1 for slot in converted if not 0 <= slot < n_total_slots]
        if invalid:
            raise ValueError(f"Participant {p + 1} has invalid forbidden slots {invalid}")
        forbidden.append(converted)

    fixed: list[int | None] = []
    for m, slot in enumerate(fixed_raw):
        value = None if slot == 0 else slot - 1
        if value is not None and not 0 <= value < n_total_slots:
            raise ValueError(f"Meeting {m + 1} has invalid fixed slot {slot}")
        fixed.append(value)

    precedences: list[set[int]] = []
    for post, preds in enumerate(precedences_raw):
        converted = {pred - 1 for pred in preds if pred > 0}
        invalid = [pred + 1 for pred in converted if not 0 <= pred < n_meetings]
        if invalid:
            raise ValueError(f"Meeting {post + 1} has invalid predecessors {invalid}")
        precedences.append(converted)

    meetings_by_business: list[list[int]] = [[] for _ in range(n_business)]
    for m, (p1, p2, _) in enumerate(requested):
        meetings_by_business[p1].append(m)
        meetings_by_business[p2].append(m)

    for p, meetings in enumerate(meetings_by_business):
        if len(meetings) != n_meetings_business[p]:
            raise ValueError(
                f"Participant {p + 1}: derived {len(meetings)} meetings but "
                f"nMeetingsBusiness says {n_meetings_business[p]}"
            )

    if validate_meetingsx_business:
        if len(meetingsx_business_raw) != n_business:
            raise ValueError("meetingsxBusiness must have nBusiness sets")
        for p, meetings in enumerate(meetings_by_business):
            # Original data uses dummy value 1 and shifts meeting ids by +1.
            expected = {1} | {m + 2 for m in meetings}
            if meetingsx_business_raw[p] != expected:
                raise ValueError(
                    f"Participant {p + 1}: meetingsxBusiness mismatch; "
                    f"expected {sorted(expected)}, got {sorted(meetingsx_business_raw[p])}"
                )

    return B2BInstance(
        n_business=n_business,
        n_meetings=n_meetings,
        n_tables=n_tables,
        n_total_slots=n_total_slots,
        n_morning_slots=n_morning_slots,
        requested=requested,
        meetings_by_business=meetings_by_business,
        n_meetings_business=n_meetings_business,
        forbidden=forbidden,
        fixed=fixed,
        precedences=precedences,
        instance_name=path.name,
    )


# ---------------------------------------------------------------------------
# Precedence graph and domain reduction
# ---------------------------------------------------------------------------


def build_precedence_graph(precedences: list[set[int]]) -> PrecedenceGraphInfo:
    """Compute direct, full-closure, and source-augmented precedence graphs.

    Let ``E`` be the input edge set and let ``R`` contain the source vertices of
    ``E``.  The source-augmented set is

        E_R = E union {(r, v): r in R and r reaches v in E}.

    Thus every original precedence is retained, while new shortcut arcs are
    added only when their first endpoint is a source.  The full transitive
    closure is retained for cycle detection and graph statistics; it is not the
    edge set encoded by the ``source-closure`` variant.
    """

    n = len(precedences)
    direct = [set(preds) for preds in precedences]
    closure = [set(preds) for preds in precedences]

    # Warshall over predecessor sets: if k precedes v, all predecessors of k precede v.
    for k in range(n):
        for v in range(n):
            if k in closure[v]:
                closure[v].update(closure[k])

    successors = [set() for _ in range(n)]
    for post, preds in enumerate(direct):
        for pred in preds:
            successors[pred].add(post)

    source_nodes = tuple(v for v, preds in enumerate(direct) if not preds)
    source_set = set(source_nodes)
    source_augmented = [set(preds) for preds in direct]
    for post, transitive_preds in enumerate(closure):
        source_augmented[post].update(transitive_preds.intersection(source_set))

    cycle_nodes = tuple(v for v in range(n) if v in closure[v])
    direct_edge_count = sum(map(len, direct))
    source_augmented_edge_count = sum(map(len, source_augmented))
    return PrecedenceGraphInfo(
        direct_predecessors=direct,
        transitive_predecessors=closure,
        source_augmented_predecessors=source_augmented,
        successors=successors,
        source_nodes=source_nodes,
        cycle_nodes=cycle_nodes,
        direct_edge_count=direct_edge_count,
        transitive_edge_count=sum(map(len, closure)),
        source_added_edge_count=(
            source_augmented_edge_count - direct_edge_count
        ),
        source_augmented_edge_count=source_augmented_edge_count,
    )


def _session_slots(inst: B2BInstance, m: int) -> set[int]:
    session = inst.requested[m][2]
    if session == 1:
        return set(inst.morning_slots)
    if session == 2:
        return set(inst.afternoon_slots)
    return set(range(inst.n_total_slots))


def original_eligible_slots(inst: B2BInstance, m: int) -> set[int]:
    """Unary domain from session, fixed meeting, and both participants' forbidden slots."""

    p1, p2, _ = inst.requested[m]
    slots = _session_slots(inst, m)
    fixed = inst.fixed[m]
    if fixed is not None:
        slots.intersection_update({fixed})
    slots.difference_update(inst.forbidden[p1])
    slots.difference_update(inst.forbidden[p2])
    return slots


def reduce_domains_with_precedence(
    inst: B2BInstance,
    graph: PrecedenceGraphInfo,
) -> tuple[list[list[int]], int, int]:
    """Apply unary filtering and precedence arc consistency to schedule domains.

    For each direct edge pred < post, repeatedly remove unsupported values from both
    domains. This propagates earliest/latest bounds through complete precedence chains.
    The transformation is satisfiability preserving.
    """

    domains = [original_eligible_slots(inst, m) for m in range(inst.n_meetings)]
    initial_count = sum(len(d) for d in domains)

    # Any directed cycle of strict precedences makes the instance unsatisfiable.
    if graph.cycle_nodes:
        if graph.cycle_nodes:
            domains[graph.cycle_nodes[0]].clear()
        return [sorted(d) for d in domains], initial_count, sum(len(d) for d in domains)

    edges = [
        (pred, post)
        for post, preds in enumerate(graph.direct_predecessors)
        for pred in preds
    ]

    changed = True
    while changed:
        changed = False
        for pred, post in edges:
            pred_domain = domains[pred]
            post_domain = domains[post]
            if not pred_domain or not post_domain:
                continue

            max_post = max(post_domain)
            new_pred = {t for t in pred_domain if t < max_post}
            if new_pred != pred_domain:
                domains[pred] = pred_domain = new_pred
                changed = True
                if not pred_domain:
                    continue

            min_pred = min(pred_domain)
            new_post = {t for t in post_domain if t > min_pred}
            if new_post != post_domain:
                domains[post] = new_post
                changed = True

    reduced_count = sum(len(d) for d in domains)
    return [sorted(d) for d in domains], initial_count, reduced_count


# ---------------------------------------------------------------------------
# Combined optimized MaxSAT/SAT encoding
# ---------------------------------------------------------------------------


class B2BSATModel:
    """Combined B2B encoder with precedence-graph and variable-domain reduction.

    Variants follow the paper:
      basic  : equations (19)-(42)
      imp1   : basic + (43)
      imp2   : basic + (44)
      imp12  : basic + (43) + (44)
      imp12+ : imp12 + (45) and clustered capacity (46)-(47)

    Unary restrictions (sessions, fixed meetings, forbidden slots) and graph-derived
    earliest/latest bounds are compiled into variable domains. No schedule variable is
    created for a removed meeting-slot pair.

    Objective in this branch:
        minimize max_{p in P*} B(p) - min_{p in P*} B(p),
    where B(p) is the total number of idle time slots strictly between the first
    and last meetings of participant p and P* contains exactly the participants
    with at least two meetings. Equivalently, if the ordered meeting slots are
    t_1 < ... < t_k, then B(p) = sum_i (t_{i+1} - t_i - 1).
    The optional ``lexicographic`` mode first proves this range optimum and then
    minimizes sum_p B(p) without relaxing the primary optimum.
    ``fairness_limit`` is only an optional hard upper bound on that gap.
    """

    def __init__(
        self,
        inst: B2BInstance,
        fairness_limit: int | None = None,
        precedence_mode: PrecedenceMode = "staircase",
        encoding_variant: EncodingVariant = "imp12+",
        objective_mode: ObjectiveMode = "idle-range",
        precedence_edge_mode: PrecedenceEdgeMode = "direct",
    ) -> None:
        if precedence_mode not in VALID_PRECEDENCE_MODES:
            raise ValueError(f"Unknown precedence_mode={precedence_mode!r}")
        if precedence_edge_mode not in VALID_PRECEDENCE_EDGE_MODES:
            raise ValueError(
                f"Unknown precedence_edge_mode={precedence_edge_mode!r}"
            )
        if encoding_variant not in VALID_ENCODING_VARIANTS:
            raise ValueError(f"Unknown encoding_variant={encoding_variant!r}")
        if objective_mode not in VALID_OBJECTIVE_MODES:
            raise ValueError(f"Unknown objective_mode={objective_mode!r}")
        if fairness_limit is not None and fairness_limit < 0:
            raise ValueError("fairness_limit must be non-negative or None")

        self.inst = inst
        self.fairness_limit = fairness_limit
        self.precedence_mode = precedence_mode
        self.precedence_edge_mode = precedence_edge_mode
        self.encoding_variant = encoding_variant
        self.objective_mode = objective_mode
        self.objective_participants = tuple(
            p
            for p, meeting_count in enumerate(inst.n_meetings_business)
            if meeting_count >= 2
        )
        self.graph = build_precedence_graph(inst.precedences)
        (
            self._eligible_slots,
            self.initial_schedule_candidates,
            self.reduced_schedule_candidates,
        ) = reduce_domains_with_precedence(inst, self.graph)
        self.precedence_predecessors = (
            self.graph.direct_predecessors
            if precedence_edge_mode == "direct"
            else self.graph.source_augmented_predecessors
        )

        self.vpool = IDPool()
        self.enabled_constraints: list[str] = []
        self._clusters: list[list[int]] | None = None
        self._artifacts: B2BModelArtifacts | None = None

        self._schedule_vars: dict[tuple[int, int], int] = {}
        for m, slots in enumerate(self._eligible_slots):
            for t in slots:
                self._schedule_vars[m, t] = self.vpool.id(("schedule", m, t))

        self._used_vars: dict[tuple[int, int], int] = {}
        for p, meetings in enumerate(inst.meetings_by_business):
            possible_slots = {
                t
                for m in meetings
                for t in self._eligible_slots[m]
            }
            for t in sorted(possible_slots):
                self._used_vars[p, t] = self.vpool.id(("usedSlot", p, t))

    # Public variable/domain helpers -----------------------------------

    def eligible_slots(self, m: int) -> list[int]:
        return list(self._eligible_slots[m])

    def x(self, m: int, t: int) -> int:
        """Return an existing schedule variable; removed pairs intentionally have none."""
        try:
            return self._schedule_vars[m, t]
        except KeyError as exc:
            raise KeyError(f"schedule({m},{t}) was removed by domain reduction") from exc

    def x_or_none(self, m: int, t: int) -> int | None:
        return self._schedule_vars.get((m, t))

    def used_or_none(self, p: int, t: int) -> int | None:
        return self._used_vars.get((p, t))

    def meeting_before(self, p: int, t: int) -> int:
        """True iff participant p has a meeting in some slot strictly before t."""
        return self.vpool.id(("meetingBefore", p, t))

    def meeting_after(self, p: int, t: int) -> int:
        """True iff participant p has a meeting in some slot strictly after t."""
        return self.vpool.id(("meetingAfter", p, t))

    def hole(self, p: int, t: int) -> int:
        """Backward-compatible accessor for the new per-idle-slot variable."""
        return self.vpool.id(("breakSlot", p, t))

    def sorted_hole(self, p: int, k: int) -> int:
        return self.vpool.id(("sortedBreakSlot", p, k))

    def max_break(self, k: int) -> int:
        return self.vpool.id(("maxBreakSlots", k))

    def min_break(self, k: int) -> int:
        return self.vpool.id(("minBreakSlots", k))

    def dif_break(self, k: int) -> int:
        return self.vpool.id(("difBreakSlots", k))

    def cluster_active(self, c: int, t: int) -> int:
        return self.vpool.id(("clusterActive", c, t))

    @property
    def use_implied_1(self) -> bool:
        return self.encoding_variant in {"imp1", "imp12", "imp12+"}

    @property
    def use_implied_2(self) -> bool:
        return self.encoding_variant in {"imp2", "imp12", "imp12+"}

    @property
    def use_further_improvements(self) -> bool:
        return self.encoding_variant == "imp12+"

    # Cardinality helpers ----------------------------------------------

    @staticmethod
    def _add_pairwise_atmost_one(cnf: CNF, lits: list[int]) -> None:
        for i, left in enumerate(lits):
            for right in lits[i + 1 :]:
                cnf.append([-left, -right])

    def _add_exactly_one_commander(
        self,
        cnf: CNF,
        lits: list[int],
        group_size: int = 4,
    ) -> None:
        if not lits:
            cnf.append([])
            return
        if len(lits) == 1:
            cnf.append([lits[0]])
            return
        if len(lits) <= group_size:
            cnf.append(list(lits))
            self._add_pairwise_atmost_one(cnf, lits)
            return

        commanders: list[int] = []
        for start in range(0, len(lits), group_size):
            group = lits[start : start + group_size]
            commander = self.vpool.id(("commander", tuple(group)))
            commanders.append(commander)
            self._add_pairwise_atmost_one(cnf, group)
            for lit in group:
                cnf.append([-lit, commander])
            cnf.append([-commander] + group)
        self._add_exactly_one_commander(cnf, commanders, group_size)

    def _add_atmost_seqcounter(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0:
            cnf.append([])
        elif bound == 0:
            cnf.extend([[-lit] for lit in lits])
        elif bound < len(lits):
            enc = CardEnc.atmost(
                lits=lits,
                bound=bound,
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

    def _add_exactly_cardnet(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0 or bound > len(lits):
            cnf.append([])
        elif bound == 0:
            cnf.extend([[-lit] for lit in lits])
        elif bound == len(lits):
            cnf.extend([[lit] for lit in lits])
        else:
            enc = CardEnc.equals(
                lits=lits,
                bound=bound,
                vpool=self.vpool,
                encoding=EncType.cardnetwrk,
            )
            cnf.extend(enc.clauses)

    def _add_atmost_cardnet(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0:
            cnf.append([])
        elif bound == 0:
            cnf.extend([[-lit] for lit in lits])
        elif bound < len(lits):
            enc = CardEnc.atmost(
                lits=lits,
                bound=bound,
                vpool=self.vpool,
                encoding=EncType.cardnetwrk,
            )
            cnf.extend(enc.clauses)

    # Build entry points ------------------------------------------------

    def build_base_cnf(self) -> B2BModelArtifacts:
        if self._artifacts is not None:
            return self._artifacts

        cnf = CNF()
        self.enabled_constraints = [
            "domain reduction: sessions, fixed meetings, forbidden slots",
            "precedence domain propagation: fixed point over direct edges",
            "full precedence closure: cycle detection and graph statistics only",
        ]

        if self.graph.cycle_nodes:
            cnf.append([])
            self.enabled_constraints.append("strict precedence cycle -> UNSAT")

        self._add_assignment(cnf)                    # (20), (22)-(27) through domains
        self._add_participant_collision(cnf)         # (19)
        if self.use_further_improvements:
            self._add_cluster_capacity(cnf)          # (46), (47), replaces (21)
        else:
            self._add_capacity_over_meetings(cnf)    # (21)
        self._add_precedences(cnf)                   # (28)

        hole_lits = self._add_break_tracking(cnf)    # usedSlot + per-idle-slot cost
        sorted_lits, gap_lits = self._add_break_slot_gap_objective(
            cnf,
            hole_lits,
            fairness_cap=self.fairness_limit,
        )
        # The number of true gap literals is exactly the range of B(p) over P*,
        # where P* contains participants with at least two meetings.
        objective_lits = gap_lits
        secondary_objective_lits = [
            lit
            for participant_lits in hole_lits
            for lit in participant_lits
        ]
        objective_name = {
            "idle-range": "internal_idle_slot_range_pstar",
            "lexicographic": (
                "lexicographic_internal_idle_range_pstar"
                "_then_idle_max_then_idle_sum"
            ),
            "lex-idlesum": (
                "lexicographic_internal_idle_range_pstar_then_idle_sum"
            ),
        }[self.objective_mode]

        self._artifacts = B2BModelArtifacts(
            cnf=cnf,
            objective_lits=objective_lits,
            secondary_objective_lits=secondary_objective_lits,
            objective_name=objective_name,
            objective_mode=self.objective_mode,
            objective_participants=self.objective_participants,
            fairness_limit=self.fairness_limit,
            fairness_gap_lits=gap_lits,
            hole_lits_by_participant=hole_lits,
            sorted_hole_lits_by_participant=sorted_lits,
            n_vars=max(self.vpool.top, cnf.nv),
            n_clauses=len(cnf.clauses),
            encoding_variant=self.encoding_variant,
            precedence_mode=self.precedence_mode,
            precedence_edge_mode=self.precedence_edge_mode,
            enabled_constraints=list(self.enabled_constraints),
            initial_schedule_candidates=self.initial_schedule_candidates,
            reduced_schedule_candidates=self.reduced_schedule_candidates,
            removed_schedule_candidates=(
                self.initial_schedule_candidates - self.reduced_schedule_candidates
            ),
            precedence_direct_edges=self.graph.direct_edge_count,
            precedence_transitive_edges=self.graph.transitive_edge_count,
            precedence_source_added_edges=self.graph.source_added_edge_count,
            precedence_encoded_edges=sum(
                map(len, self.precedence_predecessors)
            ),
            precedence_cycle_nodes=self.graph.cycle_nodes,
        )
        return self._artifacts

    def build_wcnf(self) -> WCNF:
        """Build phase-1 MaxSAT minimizing the internal-idle range over P*."""

        artifacts = self.build_base_cnf()
        wcnf = WCNF()
        for clause in artifacts.cnf.clauses:
            wcnf.append(clause)
        for lit in artifacts.objective_lits:
            wcnf.append([-lit], weight=1)
        return wcnf

    # Feasibility -------------------------------------------------------

    def _add_assignment(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(20),(22)-(27) exactly once over reduced domains")
        for m, slots in enumerate(self._eligible_slots):
            self._add_exactly_one_commander(cnf, [self.x(m, t) for t in slots])

    def _add_participant_collision(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(19) participant atMost-one per slot")
        for p, meetings in enumerate(self.inst.meetings_by_business):
            for t in range(self.inst.n_total_slots):
                lits = [
                    lit
                    for m in meetings
                    if (lit := self.x_or_none(m, t)) is not None
                ]
                if len(lits) > 1:
                    self._add_pairwise_atmost_one(cnf, lits)

    def _add_capacity_over_meetings(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(21) capacity over reduced schedule variables")
        for t in range(self.inst.n_total_slots):
            lits = [
                lit
                for m in range(self.inst.n_meetings)
                if (lit := self.x_or_none(m, t)) is not None
            ]
            self._add_atmost_seqcounter(cnf, lits, self.inst.n_tables)

    def _compute_meeting_clusters(self) -> list[list[int]]:
        if self._clusters is not None:
            return self._clusters

        unassigned = set(range(self.inst.n_meetings))
        clusters: list[list[int]] = []
        while unassigned:
            best: list[int] = []
            for meetings in self.inst.meetings_by_business:
                candidate = sorted(unassigned.intersection(meetings))
                if len(candidate) > len(best):
                    best = candidate
            if not best:
                best = [min(unassigned)]
            clusters.append(best)
            unassigned.difference_update(best)

        self._clusters = clusters
        return clusters

    def _add_cluster_capacity(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(46),(47) clustered table capacity with full channeling")
        clusters = self._compute_meeting_clusters()

        for t in range(self.inst.n_total_slots):
            active_clusters: list[int] = []
            for c, meetings in enumerate(clusters):
                member_lits = [
                    lit
                    for m in meetings
                    if (lit := self.x_or_none(m, t)) is not None
                ]
                if not member_lits:
                    continue
                if len(member_lits) == 1:
                    active_clusters.append(member_lits[0])
                    continue

                cluster_lit = self.cluster_active(c, t)
                active_clusters.append(cluster_lit)
                for lit in member_lits:
                    cnf.append([-lit, cluster_lit])
                cnf.append([-cluster_lit] + member_lits)

            self._add_atmost_seqcounter(cnf, active_clusters, self.inst.n_tables)

    # Precedence --------------------------------------------------------

    def _add_precedences(self, cnf: CNF) -> None:
        edge_description = (
            "direct E"
            if self.precedence_edge_mode == "direct"
            else "source-augmented E_R"
        )
        if self.precedence_mode == "traditional":
            self.enabled_constraints.append(
                f"(28) traditional pairwise precedence over {edge_description}"
            )
            for post, preds in enumerate(self.precedence_predecessors):
                for pred in preds:
                    for pred_t in self._eligible_slots[pred]:
                        pred_lit = self.x(pred, pred_t)
                        for post_t in self._eligible_slots[post]:
                            if pred_t >= post_t:
                                cnf.append([-pred_lit, -self.x(post, post_t)])
        else:
            self.enabled_constraints.append(
                "(28) staircase/support precedence over reduced domains "
                f"and {edge_description}"
            )
            for post, preds in enumerate(self.precedence_predecessors):
                for pred in preds:
                    pred_slots = self._eligible_slots[pred]
                    for post_t in self._eligible_slots[post]:
                        support = [self.x(pred, pred_t) for pred_t in pred_slots if pred_t < post_t]
                        cnf.append([-self.x(post, post_t)] + support)

    # Break semantics and implied constraints --------------------------

    def _add_break_tracking(self, cnf: CNF) -> list[list[int]]:
        """Create one cost literal for every internal idle time slot.

        For participant p and slot t, ``breakSlot[p,t]`` is true exactly when:
          * p has at least one meeting before t,
          * p has no meeting at t, and
          * p has at least one meeting after t.

        Thus a contiguous idle interval of length q contributes q true literals.
        For example, break lengths [1, 1, 2, 1] contribute 5, not 4.
        """
        self.enabled_constraints.append(
            "usedSlot channeling + exact per-idle-slot break cost"
        )
        break_slot_lits_by_participant: list[list[int]] = []

        for p, meetings in enumerate(self.inst.meetings_by_business):
            # Channel schedule variables to participant-used-slot variables.
            for t in range(self.inst.n_total_slots):
                used = self.used_or_none(p, t)
                if used is None:
                    continue
                scheduled = [
                    lit
                    for m in meetings
                    if (lit := self.x_or_none(m, t)) is not None
                ]
                for lit in scheduled:
                    cnf.append([-lit, used])
                cnf.append([-used] + scheduled)

            if self.use_implied_1:
                self._add_implied_constraint_1(cnf, p)

            break_slots_p: list[int] = []
            for t in range(1, self.inst.n_total_slots - 1):
                previous_used = [
                    used
                    for tau in range(t)
                    if (used := self.used_or_none(p, tau)) is not None
                ]
                future_used = [
                    used
                    for tau in range(t + 1, self.inst.n_total_slots)
                    if (used := self.used_or_none(p, tau)) is not None
                ]
                if not previous_used or not future_used:
                    continue

                before = self.meeting_before(p, t)
                after = self.meeting_after(p, t)

                # before <-> OR(previous_used)
                for used in previous_used:
                    cnf.append([-used, before])
                cnf.append([-before] + previous_used)

                # after <-> OR(future_used)
                for used in future_used:
                    cnf.append([-used, after])
                cnf.append([-after] + future_used)

                current_used = self.used_or_none(p, t)
                break_slot = self.hole(p, t)
                break_slots_p.append(break_slot)

                # break_slot -> before AND after AND NOT current_used
                cnf.append([-break_slot, before])
                cnf.append([-break_slot, after])
                if current_used is not None:
                    cnf.append([-break_slot, -current_used])

                # before AND after AND NOT current_used -> break_slot
                reverse = [-before, -after, break_slot]
                if current_used is not None:
                    reverse.insert(2, current_used)
                cnf.append(reverse)

            break_slot_lits_by_participant.append(break_slots_p)

        if self.use_implied_2:
            self._add_implied_constraint_2(cnf)

        return break_slot_lits_by_participant

    def _add_implied_constraint_1(self, cnf: CNF, p: int) -> None:
        marker = "(43) exactly |Mp| reduced usedSlot variables"
        if marker not in self.enabled_constraints:
            self.enabled_constraints.append(marker)
        lits = [
            used
            for t in range(self.inst.n_total_slots)
            if (used := self.used_or_none(p, t)) is not None
        ]
        self._add_exactly_cardnet(cnf, lits, self.inst.n_meetings_business[p])

    def _add_implied_constraint_2(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(44) atMost 2|L| busy participants per slot")
        bound = 2 * self.inst.n_tables
        for t in range(self.inst.n_total_slots):
            lits = [
                used
                for p in range(self.inst.n_business)
                if (used := self.used_or_none(p, t)) is not None
            ]
            if self.use_further_improvements:
                self._add_even_busy_cardinality(cnf, lits, bound)
            else:
                self._add_atmost_cardnet(cnf, lits, bound)

    def _add_even_busy_cardinality(self, cnf: CNF, lits: list[int], bound: int) -> None:
        """Encode (44) and the even-cardinality improvement (45) exactly.

        The paper expresses evenness through cardinality-network outputs. PySAT does
        not expose fully reified sorting outputs through CardEnc, so this implementation
        keeps the paper's atMost-cardinality constraint and adds an exact linear XOR
        parity chain. This is logically equivalent to forbidding every odd count.
        """

        marker = "(45) exact even number of busy participants"
        if marker not in self.enabled_constraints:
            self.enabled_constraints.append(marker)

        self._add_atmost_cardnet(cnf, lits, bound)
        if not lits:
            return
        if len(lits) == 1:
            cnf.append([-lits[0]])
            return

        parity = lits[0]
        for index, lit in enumerate(lits[1:], start=1):
            next_parity = self.vpool.id(("busyParity", tuple(lits), index))
            # next_parity <-> parity XOR lit
            cnf.append([parity, lit, -next_parity])
            cnf.append([-parity, -lit, -next_parity])
            cnf.append([parity, -lit, next_parity])
            cnf.append([-parity, lit, next_parity])
            parity = next_parity
        cnf.append([-parity])

    @staticmethod
    def _add_comparator(cnf: CNF, left: int, right: int, high: int, low: int) -> None:
        """Exact Boolean comparator: high=left OR right, low=left AND right."""

        cnf.append([-left, high])
        cnf.append([-right, high])
        cnf.append([left, right, -high])
        cnf.append([left, -low])
        cnf.append([right, -low])
        cnf.append([-left, -right, low])

    def _sort_descending(self, cnf: CNF, lits: list[int], tag: tuple[object, ...]) -> list[int]:
        """Insertion sorting network with fully reified unary outputs."""

        outputs: list[int] = []
        for input_index, lit in enumerate(lits):
            carry = lit
            next_outputs: list[int] = []
            for position, existing in enumerate(outputs):
                high = self.vpool.id(("sortHigh", tag, input_index, position))
                low = self.vpool.id(("sortLow", tag, input_index, position))
                self._add_comparator(cnf, carry, existing, high, low)
                next_outputs.append(high)
                carry = low
            next_outputs.append(carry)
            outputs = next_outputs
        return outputs

    # Optimization and fairness ----------------------------------------

    def _participant_break_upper_bound(self, p: int, hole_count: int) -> int:
        """Upper bound on total internal idle slots for participant p."""
        meetings = self.inst.n_meetings_business[p]
        if meetings <= 1:
            return 0
        # At most every non-meeting slot can lie between the first and last meeting.
        theoretical = max(0, self.inst.n_total_slots - meetings)
        return min(hole_count, theoretical)

    def _add_break_slot_gap_objective(
        self,
        cnf: CNF,
        hole_lits_by_participant: list[list[int]],
        fairness_cap: int | None = None,
    ) -> tuple[list[list[int]], list[int]]:
        """Encode the range of B(p) over P* using unary thresholds.

        P* = {p : |M_p| >= 2}; participants outside P* cannot have an internal
        idle slot and are intentionally excluded from the max/min calculation.
        B(p) is the total number of internal idle slots. At threshold k:
          sortedBreakSlot[p,k] <-> B(p) >= k
          maxBreakSlots[k]     <-> OR_{p in P*} sortedBreakSlot[p,k]
          minBreakSlots[k]     <-> AND_{p in P*} sortedBreakSlot[p,k]
          difBreakSlots[k]     <-> maxBreakSlots[k] AND NOT minBreakSlots[k]

        Therefore sum_k difBreakSlots[k]
          = max_{p in P*} B(p) - min_{p in P*} B(p).
        The range of an empty or singleton P* is defined as zero.
        """
        self.enabled_constraints.append(
            "exact unary internal-idle counts and range objective over P*"
        )
        sorted_by_participant: list[list[int]] = []

        for p, holes in enumerate(hole_lits_by_participant):
            upper = self._participant_break_upper_bound(p, len(holes))
            if upper == 0:
                sorted_by_participant.append([])
                continue

            unary_outputs = self._sort_descending(cnf, holes, ("participantBreakSlots", p))
            sorted_p: list[int] = []
            for k in range(1, upper + 1):
                sorted_lit = self.sorted_hole(p, k)
                sorted_p.append(sorted_lit)
                output = unary_outputs[k - 1]
                cnf.append([-sorted_lit, output])
                cnf.append([-output, sorted_lit])
            sorted_by_participant.append(sorted_p)

        global_upper = max(
            (
                len(sorted_by_participant[p])
                for p in self.objective_participants
            ),
            default=0,
        )
        gap_lits: list[int] = []

        for k in range(1, global_upper + 1):
            max_lit = self.max_break(k)
            min_lit = self.min_break(k)
            gap_lit = self.dif_break(k)
            gap_lits.append(gap_lit)

            threshold_lits = [
                sorted_by_participant[p][k - 1]
                for p in self.objective_participants
                if k <= len(sorted_by_participant[p])
            ]

            # maxBreak[k] <-> OR of all available threshold literals.
            if threshold_lits:
                for sorted_lit in threshold_lits:
                    cnf.append([-sorted_lit, max_lit])
                cnf.append([-max_lit] + threshold_lits)
            else:
                cnf.append([-max_lit])

            # minBreak[k] <-> AND over P*. A missing threshold is constant false
            # because that objective participant cannot reach level k.
            if len(threshold_lits) != len(self.objective_participants):
                cnf.append([-min_lit])
            else:
                for sorted_lit in threshold_lits:
                    cnf.append([-min_lit, sorted_lit])
                cnf.append([min_lit] + [-lit for lit in threshold_lits])

            # difBreak[k] <-> maxBreak[k] AND NOT minBreak[k].
            cnf.append([-gap_lit, max_lit])
            cnf.append([-gap_lit, -min_lit])
            cnf.append([-max_lit, min_lit, gap_lit])

        if fairness_cap is not None:
            self._add_atmost_seqcounter(cnf, gap_lits, fairness_cap)
            self.enabled_constraints.append(
                f"optional hard IdleRange(P*) cap <= {fairness_cap}"
            )

        return sorted_by_participant, gap_lits

    # Decoding and independent validation ------------------------------

    def decode_assignment(self, sat_model: list[int]) -> list[int]:
        positives = {lit for lit in sat_model if lit > 0}
        assignment = [-1] * self.inst.n_meetings
        for m, slots in enumerate(self._eligible_slots):
            chosen = [t for t in slots if self.x(m, t) in positives]
            assignment[m] = chosen[0] if chosen else -1
        return assignment

    def encoded_objective_value(self, sat_model: list[int]) -> int:
        """Return the gap encoded by the objective literals in a SAT model."""
        artifacts = self.build_base_cnf()
        positives = {lit for lit in sat_model if lit > 0}
        return sum(1 for lit in artifacts.objective_lits if lit in positives)

    def max_break_lits(self) -> list[int]:
        """Unary literals whose true count is max_{p in P*} B(p).

        With theta(p, k) <-> B(p) >= k, _add_break_slot_gap_objective already
        asserts maxBreakSlots[k] <-> OR_{p in P*} theta(p, k). Those literals are
        monotone in k, so maxBreakSlots is the unary representation of
        max_p B(p) and summing it yields that maximum. Minimizing the sum of
        this list therefore minimizes max(k such that theta(p, k) is true).

        Only one literal per threshold level is needed, versus one per
        (participant, level) pair for ``secondary_objective_lits``.
        """
        artifacts = self.build_base_cnf()
        sorted_lits = artifacts.sorted_hole_lits_by_participant
        global_upper = max(
            (len(sorted_lits[p]) for p in artifacts.objective_participants),
            default=0,
        )
        return [self.max_break(k) for k in range(1, global_upper + 1)]

    def min_break_lits(self) -> list[int]:
        """Unary literals whose true count is min_{p in P*} B(p).

        The mirror of ``max_break_lits``: _add_break_slot_gap_objective asserts
        minBreakSlots[k] <-> AND_{p in P*} theta(p, k), so summing this list
        yields min_p B(p).

        Useful as a derived bound in phase 3. Once IdleRange and IdleMax are
        pinned to their proven optima R* and M*, IdleRange = IdleMax - IdleMin
        forces IdleMin = M* - R* exactly, so bounding this sum adds no solutions
        but gives the solver a directly propagating constraint.
        """
        artifacts = self.build_base_cnf()
        sorted_lits = artifacts.sorted_hole_lits_by_participant
        global_upper = max(
            (len(sorted_lits[p]) for p in artifacts.objective_participants),
            default=0,
        )
        return [self.min_break(k) for k in range(1, global_upper + 1)]

    def encoded_idle_min(self, sat_model: list[int]) -> int:
        """Return min_p B(p) encoded by the minBreakSlots literals."""
        positives = {lit for lit in sat_model if lit > 0}
        return sum(1 for lit in self.min_break_lits() if lit in positives)

    def encoded_idle_max(self, sat_model: list[int]) -> int:
        """Return max_p B(p) encoded by the maxBreakSlots literals."""
        positives = {lit for lit in sat_model if lit > 0}
        return sum(1 for lit in self.max_break_lits() if lit in positives)

    def secondary_max_consistency_errors(
        self,
        sat_model: list[int],
        stats: B2BSolutionStats,
        *,
        imposed_bound: int | None = None,
        solver_cost: int | None = None,
    ) -> list[str]:
        """Cross-check the encoded IdleMax against the decoded schedule."""
        encoded = self.encoded_idle_max(sat_model)
        expected = stats.max_internal_idle_slots
        errors: list[str] = []
        if encoded != expected:
            errors.append(
                "secondary objective encoding mismatch: "
                f"encoded IdleMax={encoded}, schedule IdleMax={expected}"
            )
        if imposed_bound is not None and encoded > imposed_bound:
            errors.append(
                "secondary objective-bound violation: "
                f"encoded IdleMax={encoded}, imposed bound={imposed_bound}"
            )
        if solver_cost is not None and solver_cost != encoded:
            errors.append(
                "secondary solver-cost mismatch: "
                f"solver cost={solver_cost}, encoded IdleMax={encoded}"
            )
        return errors

    def encoded_idle_sum(self, sat_model: list[int]) -> int:
        """Return IdleSum encoded by the secondary objective literals."""
        artifacts = self.build_base_cnf()
        positives = {lit for lit in sat_model if lit > 0}
        return sum(
            1
            for lit in artifacts.secondary_objective_lits
            if lit in positives
        )

    def objective_consistency_errors(
        self,
        sat_model: list[int],
        stats: B2BSolutionStats,
        *,
        imposed_bound: int | None = None,
        solver_cost: int | None = None,
    ) -> list[str]:
        """Cross-check CNF/WCNF objective semantics against decoded schedule stats."""
        encoded = self.encoded_objective_value(sat_model)
        expected = stats.fairness_gap
        errors: list[str] = []
        if encoded != expected:
            errors.append(
                "objective encoding mismatch: "
                f"encoded gap={encoded}, schedule gap={expected}"
            )
        if imposed_bound is not None and encoded > imposed_bound:
            errors.append(
                "objective-bound violation: "
                f"encoded gap={encoded}, imposed bound={imposed_bound}"
            )
        if solver_cost is not None and solver_cost != encoded:
            errors.append(
                "solver-cost mismatch: "
                f"solver cost={solver_cost}, encoded gap={encoded}"
            )
        return errors

    def secondary_objective_consistency_errors(
        self,
        sat_model: list[int],
        stats: B2BSolutionStats,
        *,
        imposed_bound: int | None = None,
        solver_cost: int | None = None,
    ) -> list[str]:
        """Cross-check the encoded IdleSum against the decoded schedule."""
        encoded = self.encoded_idle_sum(sat_model)
        expected = stats.total_internal_idle_slots
        errors: list[str] = []
        if encoded != expected:
            errors.append(
                "secondary objective encoding mismatch: "
                f"encoded IdleSum={encoded}, schedule IdleSum={expected}"
            )
        if imposed_bound is not None and encoded > imposed_bound:
            errors.append(
                "secondary objective-bound violation: "
                f"encoded IdleSum={encoded}, imposed bound={imposed_bound}"
            )
        if solver_cost is not None and solver_cost != encoded:
            errors.append(
                "secondary solver-cost mismatch: "
                f"solver cost={solver_cost}, encoded IdleSum={encoded}"
            )
        return errors

    def compute_stats(self, assignment: list[int]) -> B2BSolutionStats:
        meetings_per_slot: list[list[int]] = [[] for _ in range(self.inst.n_total_slots)]
        for m, t in enumerate(assignment):
            if 0 <= t < self.inst.n_total_slots:
                meetings_per_slot[t].append(m)

        participant_breaks = [0] * self.inst.n_business
        for p, meetings in enumerate(self.inst.meetings_by_business):
            slots = sorted(assignment[m] for m in meetings if assignment[m] >= 0)
            participant_breaks[p] = sum(
                max(0, right - left - 1)
                for left, right in zip(slots, slots[1:])
            )

        objective_values = [
            participant_breaks[p]
            for p in self.objective_participants
        ]
        objective_range = (
            max(objective_values) - min(objective_values)
            if len(objective_values) >= 2
            else 0
        )
        all_participant_range = (
            max(participant_breaks) - min(participant_breaks)
            if len(participant_breaks) >= 2
            else 0
        )

        return B2BSolutionStats(
            total_breaks=sum(participant_breaks),
            participant_breaks=participant_breaks,
            fairness_gap=objective_range,
            all_participant_idle_range=all_participant_range,
            objective_participants=self.objective_participants,
            meetings_per_slot=meetings_per_slot,
            busy_participants_per_slot=[2 * len(ms) for ms in meetings_per_slot],
        )

    def validate_assignment(self, assignment: list[int]) -> list[str]:
        """Check a decoded schedule against the original B2B semantics."""

        errors: list[str] = []
        if len(assignment) != self.inst.n_meetings:
            return ["assignment length does not match nMeetings"]

        for m, t in enumerate(assignment):
            if t not in original_eligible_slots(self.inst, m):
                errors.append(
                    f"meeting {m + 1} assigned to an ineligible slot "
                    f"{t + 1 if t >= 0 else t}"
                )

        for p, meetings in enumerate(self.inst.meetings_by_business):
            seen: dict[int, int] = {}
            for m in meetings:
                t = assignment[m]
                if t in seen:
                    errors.append(
                        f"participant {p + 1} collision at slot {t + 1}: "
                        f"meetings {seen[t] + 1} and {m + 1}"
                    )
                seen[t] = m

        for t in range(self.inst.n_total_slots):
            count = sum(1 for assigned_t in assignment if assigned_t == t)
            if count > self.inst.n_tables:
                errors.append(
                    f"capacity exceeded at slot {t + 1}: {count}>{self.inst.n_tables}"
                )

        for post, preds in enumerate(self.graph.direct_predecessors):
            for pred in preds:
                if assignment[pred] >= assignment[post]:
                    errors.append(
                        f"precedence violation: meeting {pred + 1} !< meeting {post + 1}"
                    )

        if self.fairness_limit is not None:
            stats = self.compute_stats(assignment)
            if stats.fairness_gap > self.fairness_limit:
                errors.append(
                    f"IdleRange(P*) {stats.fairness_gap} exceeds {self.fairness_limit}"
                )
        return errors


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the combined precedence-graph + variable-reduction B2B encoding."
    )
    parser.add_argument("instance", help="MiniZinc .dzn instance")
    parser.add_argument(
        "--precedence-mode",
        choices=sorted(VALID_PRECEDENCE_MODES),
        default="staircase",
    )
    parser.add_argument(
        "--precedence-edges",
        choices=sorted(VALID_PRECEDENCE_EDGE_MODES),
        default="direct",
    )
    parser.add_argument(
        "--encoding-variant",
        choices=sorted(VALID_ENCODING_VARIANTS),
        default="imp12+",
    )
    parser.add_argument(
        "--objective-mode",
        choices=sorted(VALID_OBJECTIVE_MODES),
        default="idle-range",
    )
    parser.add_argument(
        "--fairness",
        type=int,
        default=-1,
        help=(
            "Optional hard cap on the internal-idle range over participants "
            "with at least two meetings; "
            "negative means no cap"
        ),
    )
    parser.add_argument("--write-cnf", type=Path)
    parser.add_argument("--write-wcnf", type=Path)
    parser.add_argument("--skip-meetingsx-validation", action="store_true")
    args = parser.parse_args()

    inst = read_instance(
        args.instance,
        validate_meetingsx_business=not args.skip_meetingsx_validation,
    )
    model = B2BSATModel(
        inst=inst,
        fairness_limit=None if args.fairness < 0 else args.fairness,
        precedence_mode=args.precedence_mode,
        encoding_variant=args.encoding_variant,
        objective_mode=args.objective_mode,
        precedence_edge_mode=args.precedence_edges,
    )
    artifacts = model.build_base_cnf()

    print(f"instance={inst.instance_name}")
    print(f"variant={artifacts.encoding_variant}")
    print(f"precedence_mode={artifacts.precedence_mode}")
    print(f"precedence_edge_mode={artifacts.precedence_edge_mode}")
    print(
        "schedule_candidates="
        f"{artifacts.reduced_schedule_candidates}/{artifacts.initial_schedule_candidates} "
        f"(removed={artifacts.removed_schedule_candidates})"
    )
    print(
        "precedence_edges="
        f"direct:{artifacts.precedence_direct_edges}, "
        f"source_added:{artifacts.precedence_source_added_edges}, "
        f"encoded:{artifacts.precedence_encoded_edges}, "
        f"full_closure_reference:{artifacts.precedence_transitive_edges}"
    )
    print(f"precedence_cycle_nodes={[v + 1 for v in artifacts.precedence_cycle_nodes]}")
    print(f"vars={artifacts.n_vars}")
    print(f"clauses={artifacts.n_clauses}")
    print(f"objective={artifacts.objective_name}")
    print(f"objective_literals={len(artifacts.objective_lits)}")
    print(
        "secondary_objective_literals="
        f"{len(artifacts.secondary_objective_lits)}"
    )
    print("enabled_constraints=")
    for item in artifacts.enabled_constraints:
        print(f"  - {item}")

    if args.write_cnf is not None:
        artifacts.cnf.to_file(str(args.write_cnf))
        print(f"wrote_cnf={args.write_cnf}")
    if args.write_wcnf is not None:
        model.build_wcnf().to_file(str(args.write_wcnf))
        print(f"wrote_wcnf={args.write_wcnf}")


if __name__ == "__main__":
    _main()
