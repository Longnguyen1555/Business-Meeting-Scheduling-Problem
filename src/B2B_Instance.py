from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pysat.card import CardEnc, EncType, ITotalizer
from pysat.formula import CNF, IDPool

PrecedenceMode = Literal["traditional", "staircase"]


@dataclass(slots=True)
class B2BInstance:
    n_business: int
    n_meetings: int
    n_tables: int
    n_total_slots: int
    n_morning_slots: int
    requested: list[tuple[int, int, int]]
    meetings_by_business: list[list[int]]
    n_meetings_business: list[int]
    forbidden: list[set[int]]
    fixed: list[int | None]
    precedences: list[set[int]]
    meetingsx_business_raw: list[set[int]]
    instance_name: str

    @property
    def morning_slots(self) -> list[int]:
        return list(range(self.n_morning_slots))

    @property
    def afternoon_slots(self) -> list[int]:
        return list(range(self.n_morning_slots, self.n_total_slots))

    @property
    def max_breaks_per_participant(self) -> int:
        return max(0, (self.n_total_slots - 1) // 2)

    def meeting_label(self, meeting_id: int) -> str:
        p1, p2, _ = self.requested[meeting_id]
        return f"M{meeting_id + 1}({p1 + 1},{p2 + 1})"


@dataclass(slots=True)
class B2BSolutionStats:
    total_breaks: int
    participant_breaks: list[int]
    fairness_gap: int
    meetings_per_slot: list[list[int]]
    busy_per_slot: list[int]


@dataclass(slots=True)
class B2BModelArtifacts:
    cnf: CNF
    objective_lits: list[int]
    hole_lits_by_participant: list[list[int]]
    n_vars: int
    n_clauses: int


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _remove_comments(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("%", 1)[0].strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _extract_int(text: str, name: str) -> int:
    pattern = rf"\b{name}\s*=\s*(-?\d+)\s*;"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"Cannot find integer field: {name}")
    return int(match.group(1))


def _extract_block(text: str, name: str, open_char: str, close_char: str) -> str:
    start_pattern = rf"\b{name}\s*=\s*{re.escape(open_char)}"
    match = re.search(start_pattern, text)
    if not match:
        raise ValueError(f"Cannot find block start for field: {name}")

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

    raise ValueError(f"Cannot find block end for field: {name}")


def _extract_int_list(block: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", block)]


def _extract_set_array(block: str) -> list[set[int]]:
    result: list[set[int]] = []
    i = 0
    while i < len(block):
        if block[i] == "{":
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
                        nums = {int(x) for x in re.findall(r"-?\d+", content)}
                        result.append(nums)
                        break
                i += 1
        i += 1
    return result


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def read_instance(path: str | Path) -> B2BInstance:
    path = Path(path)
    text = _remove_comments(path.read_text(encoding="utf-8"))

    n_business = _extract_int(text, "nBusiness")
    n_meetings = _extract_int(text, "nMeetings")
    n_tables = _extract_int(text, "nTables")
    n_total_slots = _extract_int(text, "nTotalSlots")
    n_morning_slots = _extract_int(text, "nMorningSlots")

    requested_block = _extract_block(text, "requested", "[", "]")
    meetingsx_business_block = _extract_block(text, "meetingsxBusiness", "[", "]")
    n_meetings_business_block = _extract_block(text, "nMeetingsBusiness", "[", "]")
    forbidden_block = _extract_block(text, "forbidden", "[", "]")
    fixed_block = _extract_block(text, "fixed", "[", "]")
    precedences_block = _extract_block(text, "precedences", "[", "]")

    requested_numbers = _extract_int_list(requested_block)
    if len(requested_numbers) != 3 * n_meetings:
        raise ValueError(
            "requested must contain exactly 3*nMeetings integers. "
            f"Found {len(requested_numbers)} values for nMeetings={n_meetings}."
        )

    requested: list[tuple[int, int, int]] = []
    for i in range(0, len(requested_numbers), 3):
        p1 = requested_numbers[i] - 1
        p2 = requested_numbers[i + 1] - 1
        sess = requested_numbers[i + 2]
        requested.append((p1, p2, sess))

    meetingsx_business_raw = _extract_set_array(meetingsx_business_block)
    if len(meetingsx_business_raw) != n_business:
        raise ValueError(
            "meetingsxBusiness must have nBusiness sets. "
            f"Found {len(meetingsx_business_raw)} for nBusiness={n_business}."
        )

    n_meetings_business = _extract_int_list(n_meetings_business_block)
    if len(n_meetings_business) != n_business:
        raise ValueError(
            "nMeetingsBusiness must contain nBusiness values. "
            f"Found {len(n_meetings_business)} for nBusiness={n_business}."
        )

    forbidden_raw = _extract_set_array(forbidden_block)
    if len(forbidden_raw) != n_business:
        raise ValueError(
            f"forbidden must have nBusiness sets. Found {len(forbidden_raw)}."
        )
    forbidden = [{slot - 1 for slot in slots if slot > 0} for slots in forbidden_raw]

    fixed_raw = _extract_int_list(fixed_block)
    if len(fixed_raw) != n_meetings:
        raise ValueError(
            f"fixed must contain nMeetings values. Found {len(fixed_raw)}."
        )
    fixed = [None if slot == 0 else slot - 1 for slot in fixed_raw]

    precedences_raw = _extract_set_array(precedences_block)
    if len(precedences_raw) != n_meetings:
        raise ValueError(
            f"precedences must have nMeetings sets. Found {len(precedences_raw)}."
        )
    precedences = [{pred - 1 for pred in preds if pred > 0} for preds in precedences_raw]

    meetings_by_business: list[list[int]] = [[] for _ in range(n_business)]
    for m, (p1, p2, _) in enumerate(requested):
        meetings_by_business[p1].append(m)
        meetings_by_business[p2].append(m)

    for p in range(n_business):
        derived_count = len(meetings_by_business[p])
        if derived_count != n_meetings_business[p]:
            raise ValueError(
                f"Participant {p + 1}: derived {derived_count} meetings from requested, "
                f"but nMeetingsBusiness says {n_meetings_business[p]}."
            )

        shifted_expected = {1} | {m + 2 for m in meetings_by_business[p]}
        if meetingsx_business_raw[p] != shifted_expected:
            raise ValueError(
                f"Participant {p + 1}: meetingsxBusiness is inconsistent with requested. "
                f"Expected {sorted(shifted_expected)}, got {sorted(meetingsx_business_raw[p])}."
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
        meetingsx_business_raw=meetingsx_business_raw,
        instance_name=path.name,
    )


# ---------------------------------------------------------------------------
# Shared SAT model used by all SAT solvers
# ---------------------------------------------------------------------------


class B2BSATModel:
    """

    All common logic lives in this file so solver files only decide how to optimize:
    - read input through read_instance()
    - build base constraints
    - add objective bound and solve

    precedence_mode:
        - "traditional": pairwise encoding from the paper, based on schedule(pred,j') -> not schedule(post,j) for j' >= j
        - "staircase": prefix/staircase encoding using F(m,t) variables
    """

    def __init__(
        self,
        inst: B2BInstance,
        fairness_limit: int | None = None,
        precedence_mode: PrecedenceMode = "staircase",
    ) -> None:
        if precedence_mode not in {"traditional", "staircase"}:
            raise ValueError(
                f"Unknown precedence_mode={precedence_mode!r}. Use 'traditional' or 'staircase'."
            )

        self.inst = inst
        self.fairness_limit = fairness_limit
        self.precedence_mode = precedence_mode
        self.vpool = IDPool()
        self._eligible_cache: dict[int, list[int]] = {}

    # ------------------------------------------------------------------
    # Variable helpers
    # ------------------------------------------------------------------

    def x(self, m: int, t: int) -> int:
        return self.vpool.id(("x", m, t))

    def fwd(self, m: int, t: int) -> int:
        return self.vpool.id(("F", m, t))

    def used(self, p: int, t: int) -> int:
        return self.vpool.id(("U", p, t))

    def held(self, p: int, t: int) -> int:
        return self.vpool.id(("H", p, t))

    def hole(self, p: int, t: int) -> int:
        return self.vpool.id(("B", p, t))

    def sorted_hole(self, p: int, k: int) -> int:
        return self.vpool.id(("S", p, k))

    def max_break(self, k: int) -> int:
        return self.vpool.id(("MAX", k))

    def min_break(self, k: int) -> int:
        return self.vpool.id(("MIN", k))

    def dif_break(self, k: int) -> int:
        return self.vpool.id(("DIF", k))

    # ------------------------------------------------------------------
    # Slot helpers
    # ------------------------------------------------------------------

    def eligible_slots(self, m: int) -> list[int]:
        cached = self._eligible_cache.get(m)
        if cached is not None:
            return cached

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

    # ------------------------------------------------------------------
    # Building hard constraints
    # ------------------------------------------------------------------

    def build_base_cnf(self) -> B2BModelArtifacts:
        cnf = CNF()

        self._add_meeting_assignment(cnf)
        self._add_participant_collision_constraints(cnf)
        self._add_table_capacity_constraints(cnf)
        self._add_forbidden_slots(cnf)

        if self.precedence_mode == "traditional":
            self._add_precedences_traditional(cnf)
        else:
            self._add_prefix_meeting_time(cnf)
            self._add_precedences_staircase(cnf)

        hole_lits_by_participant = self._add_break_tracking(cnf)
        objective_lits = [lit for group in hole_lits_by_participant for lit in group]

        if self.fairness_limit is not None and self.fairness_limit >= 0:
            self._add_fairness_constraints(
                cnf=cnf,
                hole_lits_by_participant=hole_lits_by_participant,
                fairness_limit=self.fairness_limit,
            )

        return B2BModelArtifacts(
            cnf=cnf,
            objective_lits=objective_lits,
            hole_lits_by_participant=hole_lits_by_participant,
            n_vars=max(self.vpool.top, cnf.nv),
            n_clauses=len(cnf.clauses),
        )
    #Add constraints 20, 22-26
    def _add_meeting_assignment(self, cnf: CNF) -> None:
        for m in range(self.inst.n_meetings):
            eligible = self.eligible_slots(m)
            if not eligible:
                cnf.append([])
                continue

            lits = [self.x(m, t) for t in eligible]

            if len(lits) == 1:
                cnf.append([lits[0]])
            else:
                enc = CardEnc.equals(
                    lits=lits,
                    bound=1,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
                cnf.extend(enc.clauses)

            all_slots = set(range(self.inst.n_total_slots))
            for t in sorted(all_slots - set(eligible)):
                cnf.append([-self.x(m, t)])                                         
      
    #Add constraints (19) - At most one meeting - participant a time
    def _add_participant_collision_constraints(self, cnf: CNF) -> None:
        for p in range(self.inst.n_business):
            meetings = self.inst.meetings_by_business[p]
            if len(meetings) <= 1:
                continue

            for t in range(self.inst.n_total_slots):
                lits = [self.x(m, t) for m in meetings]
                enc = CardEnc.atmost(
                    lits=lits,
                    bound=1,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
                cnf.extend(enc.clauses)

    #Add constraints (21) - At most K meeting available in a time - location
    def _add_table_capacity_constraints(self, cnf: CNF) -> None:
        for t in range(self.inst.n_total_slots):
            lits = [self.x(m, t) for m in range(self.inst.n_meetings)]
            enc = CardEnc.atmost(
                lits=lits,
                bound=self.inst.n_tables,
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

    #Add constraints (27) - Forbidden slots
    def _add_forbidden_slots(self, cnf: CNF) -> None:
        for p in range(self.inst.n_business):
            if not self.inst.forbidden[p]:
                continue

            for t in self.inst.forbidden[p]:
                for m in self.inst.meetings_by_business[p]:
                    cnf.append([-self.x(m, t)])
                cnf.append([-self.used(p, t)])

    # ------------------------------------------------------------------
    # Two precedence variants
    # ------------------------------------------------------------------

    def _add_precedences_traditional(self, cnf: CNF) -> None:
        for post in range(self.inst.n_meetings):
            post_preds = self.inst.precedences[post]
            if not post_preds:
                continue

            post_slots = self.eligible_slots(post)
            for pred in post_preds:
                pred_slots = self.eligible_slots(pred)
                for post_t in post_slots:
                    for pred_t in pred_slots:
                        if pred_t >= post_t:
                            cnf.append([-self.x(pred, pred_t), -self.x(post, post_t)])

    def _add_prefix_meeting_time(self, cnf: CNF) -> None:
        for m in range(self.inst.n_meetings):
            f0 = self.fwd(m, 0)
            x0 = self.x(m, 0)
            cnf.append([-f0, x0])
            cnf.append([-x0, f0])

            for t in range(1, self.inst.n_total_slots):
                ft = self.fwd(m, t)
                ftm1 = self.fwd(m, t - 1)
                xt = self.x(m, t)
                cnf.append([-ftm1, ft])
                cnf.append([-xt, ft])
                cnf.append([-ft, ftm1, xt])

    def _add_precedences_staircase(self, cnf: CNF) -> None:
        for post in range(self.inst.n_meetings):
            post_preds = self.inst.precedences[post]
            if not post_preds:
                continue

            for pred in post_preds:
                for post_t in self.eligible_slots(post):
                    if post_t == 0:
                        cnf.append([-self.x(post, 0)])
                    else:
                        cnf.append([-self.x(post, post_t), self.fwd(pred, post_t - 1)])

    # ------------------------------------------------------------------
    # Break tracking and fairness
    # ------------------------------------------------------------------

    def _add_break_tracking(self, cnf: CNF) -> list[list[int]]:
        hole_lits_by_participant: list[list[int]] = []

        for p in range(self.inst.n_business):
            meetings = self.inst.meetings_by_business[p]
            used_lits = [self.used(p, t) for t in range(self.inst.n_total_slots)]

            # (29) schedule(m,t) -> usedSlot(p,t)
            for m in meetings:
                for t in range(self.inst.n_total_slots):
                    cnf.append([-self.x(m, t), self.used(p, t)])

            # (30) usedSlot(p,t) -> OR_{m in Mp} schedule(m,t)
            for t in range(self.inst.n_total_slots):
                if meetings:
                    cnf.append([-self.used(p, t)] + [self.x(m, t) for m in meetings])
                else:
                    cnf.append([-self.used(p, t)])

            # (43) implied constraint: exactly |Mp| used slots
            target = self.inst.n_meetings_business[p]
            if target == 0:
                for lit in used_lits:
                    cnf.append([-lit])
            elif target == self.inst.n_total_slots:
                for lit in used_lits:
                    cnf.append([lit])
            else:
                enc = CardEnc.equals(
                    lits=used_lits,
                    bound=target,
                    vpool=self.vpool,
                    encoding=EncType.seqcounter,
                )
                cnf.extend(enc.clauses)

            # (31)-(34) meetingHeld semantics
            h0 = self.held(p, 0)
            u0 = self.used(p, 0)
            cnf.append([u0, -h0])
            cnf.append([-u0, h0])

            for t in range(1, self.inst.n_total_slots):
                ht = self.held(p, t)
                hprev = self.held(p, t - 1)
                ut = self.used(p, t)

                cnf.append([hprev, ut, -ht])
                cnf.append([-ut, ht])
                cnf.append([-hprev, ht])

            # (35) endHole(p,t) <-> not used(t) and held(t) and used(t+1)
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

            if target <= 1 or target == self.inst.n_total_slots:
                for lit in holes_p:
                    cnf.append([-lit])

            hole_lits_by_participant.append(holes_p)

        # (44) implied constraint: at most 2*|L| busy participants at any slot
        for t in range(self.inst.n_total_slots):
            lits = [self.used(p, t) for p in range(self.inst.n_business)]
            enc = CardEnc.atmost(
                lits=lits,
                bound=2 * self.inst.n_tables,
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

        return hole_lits_by_participant

    def _add_fairness_constraints(
        self,
        cnf: CNF,
        hole_lits_by_participant: list[list[int]],
        fairness_limit: int,
    ) -> None:
        max_breaks = self.inst.max_breaks_per_participant
        if max_breaks <= 0:
            return

        top_id = self.vpool.top
        rhs_by_participant: list[list[int]] = []

        for p, holes_p in enumerate(hole_lits_by_participant):
            if not holes_p:
                rhs_by_participant.append([])
                for k in range(1, max_breaks + 1):
                    cnf.append([-self.sorted_hole(p, k)])
                continue

            ubound = min(len(holes_p), max_breaks)
            tot = ITotalizer(lits=holes_p, ubound=ubound, top_id=top_id)
            cnf.extend(tot.cnf.clauses)
            top_id = max(top_id, tot.cnf.nv)

            rhs = list(tot.rhs)
            rhs_by_participant.append(rhs)

            for k in range(1, max_breaks + 1):
                sk = self.sorted_hole(p, k)
                if k <= len(rhs):
                    cnf.append([-sk, rhs[k - 1]])
                    cnf.append([-rhs[k - 1], sk])
                else:
                    cnf.append([-sk])

        self.vpool.top = max(self.vpool.top, top_id)

        dif_lits: list[int] = []
        for k in range(1, max_breaks + 1):
            maxk = self.max_break(k)
            mink = self.min_break(k)
            difk = self.dif_break(k)
            dif_lits.append(difk)

            for p in range(self.inst.n_business):
                sk = self.sorted_hole(p, k)
                cnf.append([-sk, maxk])
                cnf.append([-mink, sk])

            cnf.append([mink, -maxk, difk])

        if fairness_limit < 0:
            return

        if fairness_limit >= len(dif_lits):
            return

        enc = CardEnc.atmost(
            lits=dif_lits,
            bound=fairness_limit,
            vpool=self.vpool,
            encoding=EncType.seqcounter,
        )
        cnf.extend(enc.clauses)

    # ------------------------------------------------------------------
    # Decoding and statistics
    # ------------------------------------------------------------------

    def decode_assignment(self, model: list[int]) -> list[int]:
        pos = {lit for lit in model if lit > 0}
        assignment = [-1] * self.inst.n_meetings

        for m in range(self.inst.n_meetings):
            for t in range(self.inst.n_total_slots):
                if self.x(m, t) in pos:
                    assignment[m] = t
                    break

        return assignment

    def compute_stats(self, assignment: list[int]) -> B2BSolutionStats:
        meetings_per_slot: list[list[int]] = [[] for _ in range(self.inst.n_total_slots)]
        for m, t in enumerate(assignment):
            if t >= 0:
                meetings_per_slot[t].append(m)

        participant_breaks: list[int] = [0] * self.inst.n_business
        for p in range(self.inst.n_business):
            slots = sorted(assignment[m] for m in self.inst.meetings_by_business[p] if assignment[m] >= 0)
            holes = 0
            for left, right in zip(slots, slots[1:]):
                if right > left + 1:
                    holes += 1
            participant_breaks[p] = holes

        total_breaks = sum(participant_breaks)
        fairness_gap = max(participant_breaks, default=0) - min(participant_breaks, default=0)
        busy_per_slot = [2 * len(ms) for ms in meetings_per_slot]

        return B2BSolutionStats(
            total_breaks=total_breaks,
            participant_breaks=participant_breaks,
            fairness_gap=fairness_gap,
            meetings_per_slot=meetings_per_slot,
            busy_per_slot=busy_per_slot,
        )


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "forum-13.original.dzn"
    inst = read_instance(target)
    model = B2BSATModel(inst=inst, fairness_limit=2, precedence_mode="staircase")
    artifacts = model.build_base_cnf()

    print(f"instance={inst.instance_name}")
    print(f"n_business={inst.n_business}")
    print(f"n_meetings={inst.n_meetings}")
    print(f"n_tables={inst.n_tables}")
    print(f"n_total_slots={inst.n_total_slots}")
    print(f"n_morning_slots={inst.n_morning_slots}")
    print(f"n_vars={artifacts.n_vars}")
    print(f"n_clauses={artifacts.n_clauses}")
