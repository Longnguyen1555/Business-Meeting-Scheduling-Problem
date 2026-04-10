from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass
from typing import List, Tuple

from pysat.card import CardEnc, EncType
from pysat.formula import CNF, IDPool
from pysat.pb import PBEnc
from pysat.solvers import Glucose3


@dataclass
class B2BInstance:
    nBusiness: int
    nMeetings: int
    nTables: int
    nTotalSlots: int
    nMorningSlots: int
    requested: list[list[int]]
    meetingsxBusiness: list[list[int]]
    nMeetingsBusiness: list[int]
    forbidden: list[list[int]]
    fixed: list[int]
    precedences: list[list[int]]


class B2BMultipleSATSolver:
    def __init__(self, inst: B2BInstance, fairness_d: int = 2) -> None:
        self.inst = inst
        self.fairness_d = fairness_d
        self.vpool = IDPool()

        nM = inst.nMeetings
        nP = inst.nBusiness
        nT = inst.nTotalSlots

        self.x = [[0] * (nT + 1) for _ in range(nM + 1)]
        self.y = [[0] * (nT + 1) for _ in range(nP + 1)]
        self.z = [[0] * (nT + 1) for _ in range(nP + 1)]
        self.h = [[0] * (nT + 1) for _ in range(nP + 1)]

        for m in range(1, nM + 1):
            for t in range(1, nT + 1):
                self.x[m][t] = self.vpool.id(("x", m, t))

        for p in range(1, nP + 1):
            for t in range(1, nT + 1):
                self.y[p][t] = self.vpool.id(("y", p, t))
                self.z[p][t] = self.vpool.id(("z", p, t))
                self.h[p][t] = self.vpool.id(("h", p, t))

        self.max_break_count = (nT - 1) // 2
        self.sortedHole = [[0] * (self.max_break_count + 1) for _ in range(nP + 1)]
        self.max_break = [0] * (self.max_break_count + 1)
        self.min_break = [0] * (self.max_break_count + 1)
        self.dif = [0] * (self.max_break_count + 1)

        for p in range(1, nP + 1):
            for j in range(1, self.max_break_count + 1):
                self.sortedHole[p][j] = self.vpool.id(("sortedHole", p, j))

        for j in range(1, self.max_break_count + 1):
            self.max_break[j] = self.vpool.id(("max_break", j))
            self.min_break[j] = self.vpool.id(("min_break", j))
            self.dif[j] = self.vpool.id(("dif", j))

    # ---------- parsing ----------

    @staticmethod
    def read_input(path: str) -> B2BInstance:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        nBusiness = int(lines[0].split("=")[1].strip().rstrip(";"))
        nMeetings = int(lines[1].split("=")[1].strip().rstrip(";"))
        nTables = int(lines[2].split("=")[1].strip().rstrip(";"))
        nTotalSlots = int(lines[3].split("=")[1].strip().rstrip(";"))
        nMorningSlots = int(lines[4].split("=")[1].strip().rstrip(";"))

        requested = [[0, 0, 0]]
        i = 6
        while i < len(lines):
            line = lines[i].strip()
            if line == "|];":
                i += 1
                break
            elif "requested" in line and "[|" in line:
                content = line.split("[|", 1)[1].rstrip(",")
                parts = content.split(",")
                if len(parts) >= 3:
                    requested.append([int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())])
            elif line.startswith("|"):
                parts = line.lstrip("|").rstrip(",").split(",")
                if len(parts) >= 3:
                    requested.append([int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())])
            i += 1

        meetingsxBusiness = [[]]
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if "meetingsxBusiness" in line and "[" in line:
                content = line.split("[", 1)[1]
                if content.startswith("{") and "," in content:
                    first_set = content.lstrip("{").rstrip(",}")
                    numbers = [int(x.strip()) - 1 for x in first_set.split(",") if x.strip()]
                    meetingsxBusiness.append(numbers[1:])
            elif line.startswith("{"):
                should_break = False
                if line.endswith("},"):
                    numbers_str = line.strip("{},")
                elif line.endswith("};") or line.endswith("}];"):
                    numbers_str = line.strip("{};]")
                    should_break = True
                else:
                    i += 1
                    continue
                numbers = [int(x.strip()) - 1 for x in numbers_str.split(",") if x.strip()]
                meetingsxBusiness.append(numbers[1:])
                if should_break:
                    i += 1
                    break
            elif line == "];" or line == "};":
                i += 1
                break
            i += 1

        nMeetingsBusiness: list[int] = []
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if "nMeetingsBusiness" in line and "[" in line:
                content = line.split("[", 1)[1].rstrip("];")
                nMeetingsBusiness = [0] + [int(x.strip()) for x in content.split(",") if x.strip()]
                i += 1
                break
            i += 1

        forbidden = [[]]
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if "forbidden" in line and "[" in line:
                content = line.split("[", 1)[1]
                if content.startswith("{"):
                    first_set = content.lstrip("{").rstrip(",}")
                    numbers = [int(x.strip()) for x in first_set.split(",") if x.strip()]
                    forbidden.append([n for n in numbers if n != 0])
            elif line.startswith("{"):
                should_break = False
                if line.endswith("},"):
                    numbers_str = line.strip("{},")
                elif line.endswith("};") or line.endswith("}];"):
                    numbers_str = line.strip("{};]")
                    should_break = True
                else:
                    i += 1
                    continue
                numbers = [int(x.strip()) for x in numbers_str.split(",") if x.strip()]
                forbidden.append([n for n in numbers if n != 0])
                if should_break:
                    i += 1
                    break
            elif line == "];" or line == "};":
                i += 1
                break
            i += 1

        fixed: list[int] = []
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if "fixed" in line and "[" in line:
                content = line.split("[", 1)[1].rstrip("];")
                fixed = [0] + [int(x.strip()) for x in content.split(",") if x.strip()]
                i += 1
                break
            i += 1

        precedences = [[]]
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if "precedences" in line and "[" in line:
                content = line.split("[", 1)[1]
                if content.startswith("{"):
                    first_set = content.lstrip("{").rstrip(",}")
                    precedences.append([int(x.strip()) for x in first_set.split(",") if x.strip()] if first_set else [])
            elif line.startswith("{"):
                should_break = False
                if line.endswith("},"):
                    numbers_str = line.strip("{},")
                elif line.endswith("};") or line.endswith("}];"):
                    numbers_str = line.strip("{};]")
                    should_break = True
                else:
                    i += 1
                    continue
                precedences.append([int(x.strip()) for x in numbers_str.split(",") if x.strip()] if numbers_str else [])
                if should_break:
                    i += 1
                    break
            elif line == "];" or line == "};":
                i += 1
                break
            i += 1

        return B2BInstance(
            nBusiness=nBusiness,
            nMeetings=nMeetings,
            nTables=nTables,
            nTotalSlots=nTotalSlots,
            nMorningSlots=nMorningSlots,
            requested=requested,
            meetingsxBusiness=meetingsxBusiness,
            nMeetingsBusiness=nMeetingsBusiness,
            forbidden=forbidden,
            fixed=fixed,
            precedences=precedences,
        )

    # ---------- base hard encoding ----------

    def build_hard_cnf(self) -> CNF:
        cnf = CNF()
        I = self.inst

        # (19) at most one meeting involving participant p at slot t
        for p in range(1, I.nBusiness + 1):
            for t in range(1, I.nTotalSlots + 1):
                lits = [self.x[m][t] for m in I.meetingsxBusiness[p]]
                if len(lits) > 1:
                    enc = CardEnc.atmost(lits=lits, bound=1, vpool=self.vpool, encoding=EncType.seqcounter)
                    cnf.extend(enc.clauses)

        # (20), (22), (24) each meeting exactly once in allowed session
        for m in range(1, I.nMeetings + 1):
            if I.requested[m][2] == 3:
                lits = [self.x[m][t] for t in range(1, I.nTotalSlots + 1)]
            elif I.requested[m][2] == 1:
                lits = [self.x[m][t] for t in range(1, I.nMorningSlots + 1)]
            else:
                lits = [self.x[m][t] for t in range(I.nMorningSlots + 1, I.nTotalSlots + 1)]
            enc = CardEnc.equals(lits=lits, bound=1, vpool=self.vpool, encoding=EncType.seqcounter)
            cnf.extend(enc.clauses)

        # (21) at most nTables meetings in a slot
        for t in range(1, I.nTotalSlots + 1):
            lits = [self.x[m][t] for m in range(1, I.nMeetings + 1)]
            if len(lits) > I.nTables:
                enc = CardEnc.atmost(lits=lits, bound=I.nTables, vpool=self.vpool, encoding=EncType.seqcounter)
                cnf.extend(enc.clauses)

        # (23), (25) explicit AM/PM forbidding
        for m in range(1, I.nMeetings + 1):
            if I.requested[m][2] == 1:
                for t in range(I.nMorningSlots + 1, I.nTotalSlots + 1):
                    cnf.append([-self.x[m][t]])
            elif I.requested[m][2] == 2:
                for t in range(1, I.nMorningSlots + 1):
                    cnf.append([-self.x[m][t]])

        # (26) fixed meetings
        for m in range(1, I.nMeetings + 1):
            if I.fixed[m] != 0:
                cnf.append([self.x[m][I.fixed[m]]])

        # (27) forbidden slots for participants
        for p in range(1, I.nBusiness + 1):
            for t in I.forbidden[p]:
                cnf.append([-self.y[p][t]])

        # (28) precedence constraints using suffix variables
        for m in range(1, I.nMeetings + 1):
            for prec in I.precedences[m]:
                sfx = [0] * (I.nTotalSlots + 1)
                sfx[I.nTotalSlots] = self.x[prec][I.nTotalSlots]
                for t in range(I.nTotalSlots - 1, 0, -1):
                    sfx[t] = self.vpool.id(("sfx", prec, m, t))
                    cnf.append([-self.x[prec][t], sfx[t]])
                    cnf.append([-sfx[t + 1], sfx[t]])
                    cnf.append([self.x[prec][t], sfx[t + 1], -sfx[t]])
                for t in range(1, I.nTotalSlots + 1):
                    cnf.append([-self.x[m][t], -sfx[t]])

        # (29), (30) relation between x and y
        for m in range(1, I.nMeetings + 1):
            p1, p2 = I.requested[m][0], I.requested[m][1]
            for t in range(1, I.nTotalSlots + 1):
                cnf.append([-self.x[m][t], self.y[p1][t]])
                cnf.append([-self.x[m][t], self.y[p2][t]])

        for p in range(1, I.nBusiness + 1):
            for t in range(1, I.nTotalSlots + 1):
                lits = [self.x[m][t] for m in I.meetingsxBusiness[p]]
                cnf.append([-self.y[p][t]] + lits)

        # (31)-(35) usedSlot/meetingHeld/endHole encoding
        for p in range(1, I.nBusiness + 1):
            cnf.append([self.y[p][1], -self.z[p][1]])
            for t in range(2, I.nTotalSlots + 1):
                cnf.append([self.z[p][t - 1], self.y[p][t], -self.z[p][t]])
            for t in range(1, I.nTotalSlots + 1):
                cnf.append([-self.y[p][t], self.z[p][t]])
            for t in range(2, I.nTotalSlots + 1):
                cnf.append([-self.z[p][t - 1], self.z[p][t]])
            for t in range(1, I.nTotalSlots):
                cnf.append([-self.y[p][t + 1], self.h[p][t], -self.z[p][t], self.y[p][t]])

        # (36) sortedHole unary representation of number of breaks from h
        for p in range(1, I.nBusiness + 1):
            end_lits = [self.h[p][t] for t in range(1, I.nTotalSlots)]
            for j in range(1, self.max_break_count + 1):
                s = self.sortedHole[p][j]

                atleast_j = CardEnc.atleast(lits=end_lits, bound=j, vpool=self.vpool, encoding=EncType.seqcounter)
                for c in atleast_j.clauses:
                    cnf.append([-s] + c)

                atmost_jm1 = CardEnc.atmost(lits=end_lits, bound=j - 1, vpool=self.vpool, encoding=EncType.seqcounter)
                for c in atmost_jm1.clauses:
                    cnf.append([s] + c)

                if j < self.max_break_count:
                    cnf.append([-self.sortedHole[p][j + 1], self.sortedHole[p][j]])

        # (37), (38), max/min unary bounds
        for p in range(1, I.nBusiness + 1):
            for j in range(1, self.max_break_count + 1):
                cnf.append([-self.sortedHole[p][j], self.max_break[j]])
                cnf.append([-self.min_break[j], self.sortedHole[p][j]])

        for j in range(1, self.max_break_count):
            cnf.append([-self.max_break[j + 1], self.max_break[j]])
            cnf.append([-self.min_break[j + 1], self.min_break[j]])

        # (39), (40) fairness
        for j in range(1, self.max_break_count + 1):
            cnf.append([self.min_break[j], -self.max_break[j], self.dif[j]])

        if self.max_break_count > 0:
            fairness_lits = [self.dif[j] for j in range(1, self.max_break_count + 1)]
            enc = CardEnc.atmost(
                lits=fairness_lits,
                bound=min(self.fairness_d, len(fairness_lits)),
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

        # (43) implied constraint 1
        for p in range(1, I.nBusiness + 1):
            lits = [self.y[p][t] for t in range(1, I.nTotalSlots + 1)]
            enc = CardEnc.equals(
                lits=lits,
                bound=I.nMeetingsBusiness[p],
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

        # (44) implied constraint 2
        for t in range(1, I.nTotalSlots + 1):
            lits = [self.y[p][t] for p in range(1, I.nBusiness + 1)]
            enc = CardEnc.atmost(
                lits=lits,
                bound=2 * I.nTables,
                vpool=self.vpool,
                encoding=EncType.seqcounter,
            )
            cnf.extend(enc.clauses)

        return cnf

    # ---------- objective bound ----------

    def objective_lits(self) -> list[int]:
        lits: list[int] = []
        for p in range(1, self.inst.nBusiness + 1):
            for j in range(1, self.max_break_count + 1):
                lits.append(self.sortedHole[p][j])
        return lits

    def add_objective_bound(self, cnf: CNF, bound: int) -> None:
        lits = self.objective_lits()
        if not lits:
            return
        enc = CardEnc.atmost(lits=lits, bound=bound, vpool=self.vpool, encoding=EncType.seqcounter)
        cnf.extend(enc.clauses)

    # ---------- decoding ----------

    def decode_schedule(self, model: list[int]) -> tuple[list[int], int]:
        pos = {v for v in model if v > 0}
        meeting_slots = [0] * (self.inst.nMeetings + 1)
        total_breaks = 0

        for m in range(1, self.inst.nMeetings + 1):
            for t in range(1, self.inst.nTotalSlots + 1):
                if self.x[m][t] in pos:
                    meeting_slots[m] = t
                    break

        for p in range(1, self.inst.nBusiness + 1):
            for j in range(1, self.max_break_count + 1):
                if self.sortedHole[p][j] in pos:
                    total_breaks += 1

        return meeting_slots, total_breaks

    # ---------- solve ----------

    def solve_multiple(self, verbose: bool = False) -> tuple[list[int] | None, int | None, float]:
        t0 = time.time()
        base_cnf = self.build_hard_cnf()

        with Glucose3(bootstrap_with=base_cnf.clauses) as solver:
            if not solver.solve():
                return None, None, time.time() - t0
            model = solver.get_model()

        best_schedule, best_obj = self.decode_schedule(model)
        if verbose:
            print(f"[MultipleSAT] initial feasible total_breaks = {best_obj}")

        for bound in range(best_obj - 1, -1, -1):
            trial = CNF(from_clauses=base_cnf.clauses)
            self.add_objective_bound(trial, bound)

            with Glucose3(bootstrap_with=trial.clauses) as solver:
                sat = solver.solve()
                if verbose:
                    print(f"[MultipleSAT] try total_breaks <= {bound} -> {'SAT' if sat else 'UNSAT'}")
                if not sat:
                    break
                model = solver.get_model()

            best_schedule, best_obj = self.decode_schedule(model)

        return best_schedule, best_obj, time.time() - t0


def solve_instance(path: str, fairness_d: int = 2, verbose: bool = False):
    inst = B2BMultipleSATSolver.read_input(path)
    solver = B2BMultipleSATSolver(inst, fairness_d=fairness_d)
    return solver.solve_multiple(verbose=verbose)


def write_solution(path: str, out_path: str, fairness_d: int = 2, verbose: bool = False) -> None:
    schedule, total_breaks, runtime = solve_instance(path, fairness_d=fairness_d, verbose=verbose)
    base = os.path.basename(path)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Input: {base}\n")
        f.write("=" * 60 + "\n")
        f.write(f"Runtime: {runtime:.4f} seconds\n")
        f.write(f"Fairness d: {fairness_d}\n")
        f.write("=" * 60 + "\n\n")

        if schedule is None:
            f.write("UNSAT\n")
            return

        f.write(f"Optimal total breaks: {total_breaks}\n")
        f.write("SCHEDULE:\n")
        for m in range(1, len(schedule)):
            f.write(f"Meeting {m} -> Time slot {schedule[m]}\n")


def main() -> None:
    input_files = sorted(glob.glob("./input/*.dzn"))
    os.makedirs("./multiple_sat_output", exist_ok=True)

    for idx, input_file in enumerate(input_files, start=1):
        base_name = os.path.basename(input_file)
        output_file = f"./multiple_sat_output/{base_name}"

        print("\n" + "=" * 60)
        print(f"Processing: {base_name}")
        print(f"Test number: {idx}")
        print("=" * 60)

        if "original" not in input_file:
            print("Skipping non-original instance")
            continue

        write_solution(input_file, output_file, fairness_d=2, verbose=True)
        print(f"Output written to: {output_file}")


if __name__ == "__main__":
    main()
