from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Iterable

from pysat.card import CardEnc, EncType, ITotalizer
from pysat.formula import CNF, IDPool

PrecedenceMode = Literal["traditional", "staircase"]
EncodingVariant = Literal["basic", "imp1", "imp2", "imp12", "imp12+"]

VALID_PRECEDENCE_MODES = {"traditional", "staircase"}
VALID_ENCODING_VARIANTS = {"basic", "imp1", "imp2", "imp12", "imp12+"}

@dataclass
class B2BInstance:
    """Parsed B2B instance, using zero-based indices internally."""

    n_business: int
    n_meetings: int
    n_tables: int
    n_total_slots: int
    n_morning_slots: int
    requested: list[tuple[int, int, int]]  # (p1, p2, session), p1/p2 zero-based
    meetings_by_business: list[list[int]]
    n_meetings_business: list[int]
    forbidden: list[set[int]]             # zero-based slots
    fixed: list[int | None]               # zero-based slot or None
    precedences: list[set[int]]           # precedences[post] = set(pred)
    instance_name: str

    @property
    def morning_slots(self) -> list[int]:
        return list(range(self.n_morning_slots))

    @property
    def afternoon_slots(self) -> list[int]:
        return list(range(self.n_morning_slots, self.n_total_slots))

    @property
    def max_breaks_per_participant(self) -> int:
        # A break needs at least: meeting, idle-block, meeting. Hence floor((|T|-1)/2).
        return max(0, (self.n_total_slots - 1) // 2)

    def meeting_label(self, meeting_id: int) -> str:
        p1, p2, _ = self.requested[meeting_id]
        return f"M{meeting_id + 1}({p1 + 1},{p2 + 1})"


@dataclass
class B2BSolutionStats:
    total_breaks: int
    participant_breaks: list[int]
    fairness_gap: int
    meetings_per_slot: list[list[int]]
    busy_participants_per_slot: list[int]


@dataclass
class B2BModelArtifacts:
    cnf: CNF
    objective_lits: list[int]
    hole_lits_by_participant: list[list[int]]
    sorted_hole_lits_by_participant: list[list[int]]
    n_vars: int
    n_clauses: int
    encoding_variant: str
    precedence_mode: str
    enabled_constraints: list[str]


# ---------------------------------------------------------------------------
# MiniZinc .dzn parser
# ---------------------------------------------------------------------------


def _remove_comments(text: str) -> str:
    clean_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("%", 1)[0].strip()
        if line:
            clean_lines.append(line)
    return "\n".join(clean_lines)


def _extract_int(text: str, name: str) -> int:
    match = re.search(rf"\b{name}\s*=\s*(-?\d+)\s*;", text)
    if not match:
        raise ValueError(f"Cannot find integer field {name!r}")
    return int(match.group(1))


def _extract_block(text: str, name: str, open_char: str, close_char: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*{re.escape(open_char)}", text)
    if not match:
        raise ValueError(f"Cannot find block field {name!r}")

    i = match.end()
    start = i
    depth = 1
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
        while i < len(block) and depth > 0:
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    content = block[start:i]
                    result.append({int(x) for x in re.findall(r"-?\d+", content)})
                    break
            i += 1
        i += 1
    return result


def read_instance(path: str | Path, *, validate_meetingsx_business: bool = True) -> B2BInstance:
    """Read one B2B .dzn file.

    The original MiniZinc data is one-based. This parser converts participants,
    meetings and slots to zero-based indices in all internal structures.
    """

    path = Path(path)
    text = _remove_comments(path.read_text(encoding="utf-8"))

    n_business = _extract_int(text, "nBusiness")
    n_meetings = _extract_int(text, "nMeetings")
    n_tables = _extract_int(text, "nTables")
    n_total_slots = _extract_int(text, "nTotalSlots")
    n_morning_slots = _extract_int(text, "nMorningSlots")

    requested_nums = _extract_int_list(_extract_block(text, "requested", "[", "]"))
    if len(requested_nums) != 3 * n_meetings:
        raise ValueError(
            f"requested must contain 3*nMeetings values; got {len(requested_nums)} for nMeetings={n_meetings}"
        )

    requested: list[tuple[int, int, int]] = []
    for i in range(0, len(requested_nums), 3):
        p1 = requested_nums[i] - 1
        p2 = requested_nums[i + 1] - 1
        session = requested_nums[i + 2]
        if not (0 <= p1 < n_business and 0 <= p2 < n_business):
            raise ValueError(f"Invalid participant in requested triple at meeting {i // 3 + 1}")
        requested.append((p1, p2, session))

    meetingsx_business_raw = _extract_set_array(_extract_block(text, "meetingsxBusiness", "[", "]"))
    n_meetings_business = _extract_int_list(_extract_block(text, "nMeetingsBusiness", "[", "]"))
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

    forbidden = [{slot - 1 for slot in slots if slot > 0} for slots in forbidden_raw]
    fixed = [None if slot == 0 else slot - 1 for slot in fixed_raw]
    precedences = [{pred - 1 for pred in preds if pred > 0} for preds in precedences_raw]

    meetings_by_business: list[list[int]] = [[] for _ in range(n_business)]
    for m, (p1, p2, _) in enumerate(requested):
        meetings_by_business[p1].append(m)
        meetings_by_business[p2].append(m)

    for p, meetings in enumerate(meetings_by_business):
        if len(meetings) != n_meetings_business[p]:
            raise ValueError(
                f"Participant {p + 1}: derived {len(meetings)} meetings but nMeetingsBusiness says {n_meetings_business[p]}"
            )

    if validate_meetingsx_business:
        if len(meetingsx_business_raw) != n_business:
            raise ValueError("meetingsxBusiness must have nBusiness sets")
        for p, meetings in enumerate(meetings_by_business):
            # In these .dzn files, meetingsxBusiness includes dummy 1 and meeting ids shifted by +1.
            expected = {1} | {m + 2 for m in meetings}
            if meetingsx_business_raw[p] != expected:
                raise ValueError(
                    f"Participant {p + 1}: meetingsxBusiness mismatch; expected {sorted(expected)}, got {sorted(meetingsx_business_raw[p])}"
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
# Shared MaxSAT-style SAT encoding
# ---------------------------------------------------------------------------


class B2BSATModel:
    """Builds all shared CNF for B2B.

    This class deliberately contains the parser-facing data, variables and all
    constraints shared by the SAT optimization drivers. Solver files should not
    duplicate model constraints. They only add objective bounds.

    Variant semantics follow Table 3 of the B2B paper:
      basic  : base MaxSAT encoding, equations (19)-(42), no (43), no (44)
      imp1   : basic + (43)
      imp2   : basic + (44)
      imp12  : basic + (43) + (44)
      imp12+ : imp12 + further improvements (45), (46), (47)
    """

    def __init__(
        self,
        inst: B2BInstance,
        fairness_limit: int | None = 2,
        precedence_mode: PrecedenceMode = "traditional",
        encoding_variant: EncodingVariant = "imp12+",
    ) -> None:
        if precedence_mode not in VALID_PRECEDENCE_MODES:
            raise ValueError(f"Unknown precedence_mode={precedence_mode!r}")
        if encoding_variant not in VALID_ENCODING_VARIANTS:
            raise ValueError(f"Unknown encoding_variant={encoding_variant!r}")
        self.inst = inst
        self.fairness_limit = fairness_limit
        self.precedence_mode = precedence_mode
        self.encoding_variant = encoding_variant
        self.vpool = IDPool()
        self._eligible_cache: dict[int, list[int]] = {}
        self._clusters: list[list[int]] | None = None
        self.enabled_constraints: list[str] = []

    # Variable helpers ---------------------------------------------------

    def x(self, m: int, t: int) -> int:
        return self.vpool.id(("schedule", m, t))

    def used(self, p: int, t: int) -> int:
        return self.vpool.id(("usedSlot", p, t))

    def held(self, p: int, t: int) -> int:
        return self.vpool.id(("meetingHeld", p, t))

    def hole(self, p: int, t: int) -> int:
        return self.vpool.id(("endHole", p, t))

    def sorted_hole(self, p: int, k: int) -> int:
        return self.vpool.id(("sortedHole", p, k))

    def max_break(self, k: int) -> int:
        return self.vpool.id(("max", k))

    def min_break(self, k: int) -> int:
        return self.vpool.id(("min", k))

    def dif_break(self, k: int) -> int:
        return self.vpool.id(("dif", k))

    def prefix_done(self, m: int, t: int) -> int:
        return self.vpool.id(("prefixDone", m, t))

    def cluster_active(self, c: int, t: int) -> int:
        return self.vpool.id(("cluster", c, t))

    # Variant flags ------------------------------------------------------

    @property
    def use_implied_1(self) -> bool:
        return self.encoding_variant in {"imp1", "imp12", "imp12+"}

    @property
    def use_implied_2(self) -> bool:
        return self.encoding_variant in {"imp2", "imp12", "imp12+"}

    @property
    def use_further_improvements(self) -> bool:
        return self.encoding_variant == "imp12+"

    # Cardinality helpers ------------------------------------------------

    @staticmethod
    def _add_pairwise_atmost_one(cnf: CNF, lits: list[int]) -> None:
        for i in range(len(lits)):
            for j in range(i + 1, len(lits)):
                cnf.append([-lits[i], -lits[j]])

    def _add_exactly_one_commander(
    self,
    cnf: CNF,
    lits: list[int],
    group_size: int = 4,
) -> None:
        """
        Commander encoding for Exactly-One.
        Similar to the reference maxsat.py implementation.
        """

        if not lits:
            cnf.append([])
            return

        if len(lits) == 1:
            cnf.append([lits[0]])
            return

        # Small case -> direct encoding
        if len(lits) <= group_size:
            cnf.append(list(lits))
            self._add_pairwise_atmost_one(cnf, lits)
            return

        commanders: list[int] = []

        for start in range(0, len(lits), group_size):
            group = lits[start:start + group_size]

            c = self.vpool.id(("commander", tuple(group)))

            commanders.append(c)

            # local AMO
            self._add_pairwise_atmost_one(cnf, group)

            # x -> commander
            for x in group:
                cnf.append([-x, c])

            # commander -> OR(group)
            cnf.append([-c] + group)

        # recurse on commanders
        self._add_exactly_one_commander(cnf, commanders, group_size)

    def _add_atmost_seqcounter(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0:
            cnf.append([])
            return
        if bound >= len(lits):
            return
        if bound == 0:
            for lit in lits:
                cnf.append([-lit])
            return
        enc = CardEnc.atmost(lits=lits, bound=bound, vpool=self.vpool, encoding=EncType.seqcounter)
        cnf.extend(enc.clauses)

    def _add_exactly_cardnet(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0 or bound > len(lits):
            cnf.append([])
            return
        if bound == 0:
            for lit in lits:
                cnf.append([-lit])
            return
        if bound == len(lits):
            for lit in lits:
                cnf.append([lit])
            return
        enc = CardEnc.equals(lits=lits, bound=bound, vpool=self.vpool, encoding=EncType.cardnetwrk)
        cnf.extend(enc.clauses)

    def _add_atmost_cardnet(self, cnf: CNF, lits: list[int], bound: int) -> None:
        if bound < 0:
            cnf.append([])
            return
        if bound >= len(lits):
            return
        if bound == 0:
            for lit in lits:
                cnf.append([-lit])
            return
        enc = CardEnc.atmost(lits=lits, bound=bound, vpool=self.vpool, encoding=EncType.cardnetwrk)
        cnf.extend(enc.clauses)

    # Slot filtering -----------------------------------------------------

    def eligible_slots(self, m: int) -> list[int]:
        if m in self._eligible_cache:
            return self._eligible_cache[m]
        _, _, session_pref = self.inst.requested[m]
        if session_pref == 1:
            slots = set(self.inst.morning_slots)
        elif session_pref == 2:
            slots = set(self.inst.afternoon_slots)
        else:
            slots = set(range(self.inst.n_total_slots))
        fixed_slot = self.inst.fixed[m]
        if fixed_slot is not None:
            slots &= {fixed_slot}
        eligible = sorted(slots)
        self._eligible_cache[m] = eligible
        return eligible

    # Build entrypoint ---------------------------------------------------

    def build_base_cnf(self) -> B2BModelArtifacts:
        cnf = CNF()
        self.enabled_constraints = []

        self._add_assignment_and_time_restrictions(cnf)        # (20), (22)-(26)
        self._add_participant_collision(cnf)                   # (19)
        if self.use_further_improvements:
            self._add_cluster_capacity(cnf)                    # (46), (47), replacing (21)
        else:
            self._add_capacity_over_meetings(cnf)              # (21)
        self._add_forbidden_slots(cnf)                         # (27)
        self._add_precedences(cnf)                             # (28) or staircase equivalent

        hole_lits_by_participant = self._add_break_tracking(cnf)  # (29)-(35), optional (43), optional (44)/(45)
        sorted_lits_by_participant: list[list[int]] = []

        if self.fairness_limit is not None:
            sorted_lits_by_participant = self._add_sorted_holes_and_fairness(
                cnf=cnf,
                hole_lits_by_participant=hole_lits_by_participant,
                fairness_limit=self.fairness_limit,
            )
            # Paper objective (41): soft not(sortedHole). In SAT optimization,
            # minimize sum(sortedHole) by repeatedly bounding these literals.
            objective_lits = [lit for group in sorted_lits_by_participant for lit in group]
        else:
            # Homogeneity disabled: minimize direct endHole variables, equivalent to (42).
            objective_lits = [lit for group in hole_lits_by_participant for lit in group]

        return B2BModelArtifacts(
            cnf=cnf,
            objective_lits=objective_lits,
            hole_lits_by_participant=hole_lits_by_participant,
            sorted_hole_lits_by_participant=sorted_lits_by_participant,
            n_vars=max(self.vpool.top, cnf.nv),
            n_clauses=len(cnf.clauses),
            encoding_variant=self.encoding_variant,
            precedence_mode=self.precedence_mode,
            enabled_constraints=list(self.enabled_constraints),
        )

    # Feasibility constraints ------------------------------------------

    def _add_assignment_and_time_restrictions(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(20),(22)-(26) assignment/fixed-session/fixed-meeting")
        all_slots = set(range(self.inst.n_total_slots))
        for m in range(self.inst.n_meetings):
            eligible = self.eligible_slots(m)
            self._add_exactly_one_commander(cnf,[self.x(m, t) for t in eligible])
            for t in sorted(all_slots - set(eligible)):
                cnf.append([-self.x(m, t)])

    def _add_participant_collision(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(19) participant atMost-one per slot")
        for p, meetings in enumerate(self.inst.meetings_by_business):
            if len(meetings) < 2:
                continue
            for t in range(self.inst.n_total_slots):
                self._add_pairwise_atmost_one(cnf, [self.x(m, t) for m in meetings])

    def _add_capacity_over_meetings(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(21) capacity over scheduled meetings")
        for t in range(self.inst.n_total_slots):
            self._add_atmost_seqcounter(
                cnf=cnf,
                lits=[self.x(m, t) for m in range(self.inst.n_meetings)],
                bound=self.inst.n_tables,
            )

    def _add_forbidden_slots(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(27) forbidden slots")
        for p, forbidden_slots in enumerate(self.inst.forbidden):
            for t in forbidden_slots:
                if not (0 <= t < self.inst.n_total_slots):
                    continue
                for m in self.inst.meetings_by_business[p]:
                    cnf.append([-self.x(m, t)])

    # Precedence constraints -------------------------------------------

    def _add_precedences(self, cnf: CNF) -> None:
        if self.precedence_mode == "traditional":
            self.enabled_constraints.append("(28) traditional pairwise precedence")
            self._add_precedences_traditional(cnf)
        else:
            self.enabled_constraints.append("staircase precedence equivalent to (28)")
            self._add_prefix_done_semantics(cnf)
            self._add_precedences_staircase(cnf)

    def _add_precedences_traditional(self, cnf: CNF) -> None:
        for post, preds in enumerate(self.inst.precedences):
            for pred in preds:
                for post_t in self.eligible_slots(post):
                    for pred_t in self.eligible_slots(pred):
                        if pred_t >= post_t:
                            cnf.append([-self.x(pred, pred_t), -self.x(post, post_t)])

    def _add_prefix_done_semantics(self, cnf: CNF) -> None:
        # prefixDone(m,t) <-> OR_{tau <= t} schedule(m,tau)
        for m in range(self.inst.n_meetings):
            f0 = self.prefix_done(m, 0)
            x0 = self.x(m, 0)
            cnf.append([-f0, x0])
            cnf.append([-x0, f0])
            for t in range(1, self.inst.n_total_slots):
                ft = self.prefix_done(m, t)
                fprev = self.prefix_done(m, t - 1)
                xt = self.x(m, t)
                cnf.append([-fprev, ft])
                cnf.append([-xt, ft])
                cnf.append([-ft, fprev, xt])

    def _add_precedences_staircase(self, cnf: CNF) -> None:
        # schedule(post,t) -> prefixDone(pred,t-1). For t=0, post cannot be scheduled.
        for post, preds in enumerate(self.inst.precedences):
            for pred in preds:
                for post_t in self.eligible_slots(post):
                    if post_t == 0:
                        cnf.append([-self.x(post, 0)])
                    else:
                        cnf.append([-self.x(post, post_t), self.prefix_done(pred, post_t - 1)])

    # Break semantics and implied constraints --------------------------

    def _add_break_tracking(self, cnf: CNF) -> list[list[int]]:
        self.enabled_constraints.append("(29)-(35) usedSlot/meetingHeld/endHole")
        hole_lits_by_participant: list[list[int]] = []

        for p, meetings in enumerate(self.inst.meetings_by_business):
            # (29): schedule(m,t) -> usedSlot(p,t)
            for m in meetings:
                for t in range(self.inst.n_total_slots):
                    cnf.append([-self.x(m, t), self.used(p, t)])

            # (30): usedSlot(p,t) -> OR_{m in Mp} schedule(m,t)
            for t in range(self.inst.n_total_slots):
                if meetings:
                    cnf.append([-self.used(p, t)] + [self.x(m, t) for m in meetings])
                else:
                    cnf.append([-self.used(p, t)])

            if self.use_implied_1:
                self._add_implied_constraint_1_for_participant(cnf, p)

            # (31)-(34): meetingHeld prefix semantics.
            u0 = self.used(p, 0)
            h0 = self.held(p, 0)
            cnf.append([u0, -h0])      # not used(0) -> not held(0)
            cnf.append([-u0, h0])      # used(0) -> held(0)
            for t in range(1, self.inst.n_total_slots):
                hp = self.held(p, t - 1)
                ht = self.held(p, t)
                ut = self.used(p, t)
                cnf.append([hp, ut, -ht])  # not hp and not ut -> not ht
                cnf.append([-ut, ht])      # used -> held
                cnf.append([-hp, ht])      # previous held -> held

            # (35): endHole(p,t) <-> not used(p,t) and held(p,t) and used(p,t+1)
            holes_p: list[int] = []
            for t in range(self.inst.n_total_slots - 1):
                b = self.hole(p, t)
                ut = self.used(p, t)
                ht = self.held(p, t)
                unext = self.used(p, t + 1)
                cnf.append([-b, -ut])
                cnf.append([-b, ht])
                cnf.append([-b, unext])
                cnf.append([ut, -ht, -unext, b])
                holes_p.append(b)

            # Safe fixing from the CP/MIP observations: no possible break in trivial cases.
            if self.inst.n_meetings_business[p] <= 1 or self.inst.n_meetings_business[p] == self.inst.n_total_slots:
                for b in holes_p:
                    cnf.append([-b])

            hole_lits_by_participant.append(holes_p)

        if self.use_implied_2:
            self._add_implied_constraint_2(cnf)

        return hole_lits_by_participant

    def _add_implied_constraint_1_for_participant(self, cnf: CNF, p: int) -> None:
        if "(43) implied exactly used slots" not in self.enabled_constraints:
            self.enabled_constraints.append("(43) implied exactly used slots")
        self._add_exactly_cardnet(
            cnf=cnf,
            lits=[self.used(p, t) for t in range(self.inst.n_total_slots)],
            bound=self.inst.n_meetings_business[p],
        )

    def _add_implied_constraint_2(self, cnf: CNF) -> None:
        self.enabled_constraints.append("(44) implied busy participant capacity")
        bound = 2 * self.inst.n_tables
        for t in range(self.inst.n_total_slots):
            lits = [self.used(p, t) for p in range(self.inst.n_business)]
            if self.use_further_improvements:
                self._add_even_busy_cardinality_with_outputs(cnf, lits, bound)
            else:
                self._add_atmost_cardnet(cnf, lits, bound)

    def _add_even_busy_cardinality_with_outputs(self, cnf: CNF, lits: list[int], bound: int) -> None:
        """Implements (44) plus (45) using exposed unary counter outputs.

        The paper applies (45) to the output variables of a cardinality network.
        PySAT's public API does not expose those CardEnc output literals. We use an
        incremental totalizer to expose an equivalent unary representation rhs[k]
        meaning "at least k+1 busy participants". Clauses rhs[0]->rhs[1],
        rhs[2]->rhs[3], ... forbid odd cardinalities just like (45).
        """
        if bound >= len(lits):
            # Still add evenness if it can propagate, up to len(lits)-1.
            ubound = len(lits)
        else:
            ubound = bound + 1
        if ubound <= 0:
            for lit in lits:
                cnf.append([-lit])
            return

        totalizer = ITotalizer(lits=lits, ubound=ubound, top_id=self.vpool.top)
        cnf.extend(totalizer.cnf.clauses)
        self.vpool.top = max(self.vpool.top, totalizer.cnf.nv)
        rhs = list(totalizer.rhs)

        # (44): at most bound busy participants.
        if bound < len(rhs):
            cnf.append([-rhs[bound]])

        # (45): o_i -> o_{i+1} for odd i in 1,3,...,2|L|-1.
        # With zero-based rhs, i=1 maps rhs[0] -> rhs[1].
        limit = min(bound, len(rhs) - 1)
        for odd_i in range(1, limit + 1, 2):
            cnf.append([-rhs[odd_i - 1], rhs[odd_i]])

    # Further improvement 4.5.2 ----------------------------------------

    def _compute_meeting_clusters(self) -> list[list[int]]:
        if self._clusters is not None:
            return self._clusters
        unassigned = set(range(self.inst.n_meetings))
        clusters: list[list[int]] = []
        # Greedy partition Pi: each cluster is a star around a participant.
        while unassigned:
            best: list[int] = []
            for p in range(self.inst.n_business):
                candidate = sorted(unassigned.intersection(self.inst.meetings_by_business[p]))
                if len(candidate) > len(best):
                    best = candidate
            if not best:
                best = [min(unassigned)]
            clusters.append(best)
            unassigned.difference_update(best)
        self._clusters = clusters
        return clusters

def _add_cluster_capacity(self, cnf: CNF) -> None:
    """
    Further improvements (46)-(47).

    Replace table-capacity over all meetings by:
        - cluster activation variables
        - AtMost(|L|, active clusters)

    IMPORTANT:
    We encode FULL equivalence:

        cluster(c,t) <-> OR_{m in cluster c} x(m,t)

    instead of only:
        x(m,t) -> cluster(c,t)

    This gives much stronger reverse propagation and is
    significantly closer to the behavior exploited in the paper.
    """

    clusters = self._compute_meeting_clusters()

    for t in range(self.inst.n_total_slots):

        active_clusters: list[int] = []

        for c, meetings in enumerate(clusters):

            clit = self.cluster_active(c, t)
            active_clusters.append(clit)

            #
            # Forward direction:
            #
            # x(m,t) -> cluster(c,t)
            #
            for m in meetings:
                cnf.append([-self.x(m, t), clit])

            #
            # Reverse direction:
            #
            # cluster(c,t) -> OR meetings
            #
            # (-cluster v x1 v x2 ...)
            #
            cnf.append(
                [-clit] + [self.x(m, t) for m in meetings]
            )

        #
        # (47):
        #
        # AtMost(|L|, active clusters)
        #
        self._add_atmost_seqcounter(
            cnf=cnf,
            lits=active_clusters,
            bound=self.inst.n_tables,
        )



    def _add_sorted_holes_and_fairness(
        self,
        cnf: CNF,
        hole_lits_by_participant: list[list[int]],
        fairness_limit: int,
    ) -> list[list[int]]:
        self.enabled_constraints.append("(36)-(40) sorted holes and fairness")
        max_breaks = self.inst.max_breaks_per_participant
        sorted_by_participant: list[list[int]] = []

        if max_breaks == 0:
            return [[] for _ in range(self.inst.n_business)]

        for p, holes in enumerate(hole_lits_by_participant):
            if not holes:
                sorted_by_participant.append([])
                continue
            ubound = min(len(holes), max_breaks)
            totalizer = ITotalizer(lits=holes, ubound=ubound, top_id=self.vpool.top)
            cnf.extend(totalizer.cnf.clauses)
            self.vpool.top = max(self.vpool.top, totalizer.cnf.nv)
            rhs = list(totalizer.rhs)

            s_lits: list[int] = []
            for k in range(1, max_breaks + 1):
                sk = self.sorted_hole(p, k)
                s_lits.append(sk)
                if k <= len(rhs):
                    # (36): sortedHole(p,k) iff at least k endHole literals are true.
                    cnf.append([-sk, rhs[k - 1]])
                    cnf.append([-rhs[k - 1], sk])
                else:
                    cnf.append([-sk])
            sorted_by_participant.append(s_lits)

        dif_lits: list[int] = []
        for k in range(1, max_breaks + 1):
            maxk = self.max_break(k)
            mink = self.min_break(k)
            difk = self.dif_break(k)
            dif_lits.append(difk)
            for p in range(self.inst.n_business):
                sk = self.sorted_hole(p, k)
                cnf.append([-sk, maxk])   # (37): sortedHole -> max
                cnf.append([-mink, sk])   # (38): not sortedHole -> not min
            cnf.append([mink, -maxk, difk])  # (39): not min and max -> dif

        # (40): atMost(d, dif)
        self._add_atmost_seqcounter(cnf, dif_lits, fairness_limit)
        return sorted_by_participant

    # Decoding and consistency checks ----------------------------------

    def decode_assignment(self, sat_model: list[int]) -> list[int]:
        positives = {lit for lit in sat_model if lit > 0}
        assignment = [-1] * self.inst.n_meetings
        for m in range(self.inst.n_meetings):
            chosen = [t for t in range(self.inst.n_total_slots) if self.x(m, t) in positives]
            assignment[m] = chosen[0] if chosen else -1
        return assignment

    def compute_stats(self, assignment: list[int]) -> B2BSolutionStats:
        meetings_per_slot: list[list[int]] = [[] for _ in range(self.inst.n_total_slots)]
        for m, t in enumerate(assignment):
            if 0 <= t < self.inst.n_total_slots:
                meetings_per_slot[t].append(m)

        participant_breaks = [0] * self.inst.n_business
        for p, meetings in enumerate(self.inst.meetings_by_business):
            slots = sorted(assignment[m] for m in meetings if assignment[m] >= 0)
            participant_breaks[p] = sum(1 for left, right in zip(slots, slots[1:]) if right > left + 1)

        total_breaks = sum(participant_breaks)
        fairness_gap = max(participant_breaks, default=0) - min(participant_breaks, default=0)
        busy_participants_per_slot = [2 * len(meetings) for meetings in meetings_per_slot]
        return B2BSolutionStats(
            total_breaks=total_breaks,
            participant_breaks=participant_breaks,
            fairness_gap=fairness_gap,
            meetings_per_slot=meetings_per_slot,
            busy_participants_per_slot=busy_participants_per_slot,
        )

    def validate_assignment(self, assignment: list[int]) -> list[str]:
        """Independent checker used after solving. Returns a list of violations."""
        errors: list[str] = []
        if len(assignment) != self.inst.n_meetings:
            errors.append("assignment length does not match n_meetings")
            return errors

        for m, t in enumerate(assignment):
            if t not in self.eligible_slots(m):
                errors.append(f"meeting {m + 1} assigned to ineligible slot {t + 1 if t >= 0 else t}")

        for p, meetings in enumerate(self.inst.meetings_by_business):
            seen: dict[int, int] = {}
            for m in meetings:
                t = assignment[m]
                if t in seen:
                    errors.append(f"participant {p + 1} collision at slot {t + 1}: meetings {seen[t] + 1} and {m + 1}")
                seen[t] = m
            for t in self.inst.forbidden[p]:
                for m in meetings:
                    if assignment[m] == t:
                        errors.append(f"participant {p + 1} has meeting {m + 1} in forbidden slot {t + 1}")

        for t in range(self.inst.n_total_slots):
            if sum(1 for mt in assignment if mt == t) > self.inst.n_tables:
                errors.append(f"capacity exceeded at slot {t + 1}")

        for post, preds in enumerate(self.inst.precedences):
            for pred in preds:
                if assignment[pred] >= assignment[post]:
                    errors.append(f"precedence violation: meeting {pred + 1} !< meeting {post + 1}")

        if self.fairness_limit is not None:
            stats = self.compute_stats(assignment)
            if stats.fairness_gap > self.fairness_limit:
                errors.append(f"fairness gap {stats.fairness_gap} exceeds {self.fairness_limit}")
        return errors


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Parse and build the shared B2B CNF.")
    parser.add_argument("instance")
    parser.add_argument("--precedence-mode", choices=sorted(VALID_PRECEDENCE_MODES), default="traditional")
    parser.add_argument("--encoding-variant", choices=sorted(VALID_ENCODING_VARIANTS), default="imp12+")
    parser.add_argument("--fairness", type=int, default=2)
    args = parser.parse_args()

    inst = read_instance(args.instance)
    model = B2BSATModel(
        inst=inst,
        fairness_limit=None if args.fairness < 0 else args.fairness,
        precedence_mode=args.precedence_mode,
        encoding_variant=args.encoding_variant,
    )
    artifacts = model.build_base_cnf()
    print(f"instance={inst.instance_name}")
    print(f"variant={artifacts.encoding_variant}")
    print(f"precedence_mode={artifacts.precedence_mode}")
    print(f"vars={artifacts.n_vars}")
    print(f"clauses={artifacts.n_clauses}")
    print("enabled_constraints=")
    for item in artifacts.enabled_constraints:
        print(f"  - {item}")
