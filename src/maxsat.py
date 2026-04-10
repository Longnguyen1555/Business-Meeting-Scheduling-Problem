
import sys
from pysat.pb import *
from pysat.formula import CNF, WCNF
from pysat.card import CardEnc, EncType
from pysat.examples.rc2 import RC2
from math import inf
import signal
import subprocess
import time
import os
import glob

# Get all input files
input_files = sorted(glob.glob('./input/*.dzn'))
# print(f"Found {len(input_files)} input files to process")

test_counter = 0
for input_file in input_files:
    test_counter += 1
    # Already solved these tests
    if test_counter <= 0:
        continue    
    # Get base filename
    base_name = os.path.basename(input_file)
    output_file = f'./maxsat_output/{base_name}'
    
    print(f"\n{'='*60}")
    print(f"Processing: {base_name}")
    print(f"Test number: {test_counter}")
    print(f"{'='*60}")
    
    in_path = input_file
    out_path = output_file
    if 'original' not in in_path:
        continue
    # Reset variables for each input file
    cnf = CNF()
    wcnf = WCNF()
    variable_size = 0
    
    def read_input():
        with open(in_path) as f:
            lines = f.readlines()
        
        # Read the initial variables
        nBusiness = int(lines[0].split('=')[1].strip().rstrip(';'))
        nMeetings = int(lines[1].split('=')[1].strip().rstrip(';'))
        nTables = int(lines[2].split('=')[1].strip().rstrip(';'))
        nTotalSlots = int(lines[3].split('=')[1].strip().rstrip(';'))
        nMorningSlots = int(lines[4].split('=')[1].strip().rstrip(';'))
        
        # Parse the requested array
        requested = [[0, 0, 0]]
        i = 6  # Start after the blank line
        while i < len(lines):
            line = lines[i].strip()
            if line == '|];':
                i += 1
                break
            elif 'requested' in line and '[|' in line:
                # Handle the header line with data
                content = line.split('[|', 1)[1].rstrip(',')
                parts = content.split(',')
                if len(parts) >= 3:
                    requested.append([int(parts[0].strip()), 
                                    int(parts[1].strip()), 
                                    int(parts[2].strip())])
            elif line.startswith('|'):
                # Remove '|' and trailing comma
                parts = line.lstrip('|').rstrip(',').split(',')
                if len(parts) >= 3:
                    requested.append([int(parts[0].strip()), 
                                    int(parts[1].strip()), 
                                    int(parts[2].strip())])
            i += 1
        
        # Parse meetingsxBusiness if needed
        meetingsxBusiness = [[]]
        while i < len(lines):
            line = lines[i].strip()
            # Skip empty lines
            if not line:
                i += 1
                continue
            # Skip the header line "meetingsxBusiness = [" and extract first set if present
            if 'meetingsxBusiness' in line and '[' in line:
                # Extract everything after the '['
                content = line.split('[', 1)[1]
                if content.startswith('{') and ',' in content:
                    # First set is on the same line
                    first_set = content.lstrip('{').rstrip(',}')
                    numbers = [int(x.strip()) - 1 for x in first_set.split(',') if x.strip()]
                    numbers = numbers[1:]
                    meetingsxBusiness.append(numbers)
            elif line.startswith('{'):
                # Regular set line
                should_break = False
                if line.endswith('},'):
                    numbers_str = line.strip('{},')
                elif line.endswith('};') or line.endswith('}];'):
                    numbers_str = line.strip('{};]')
                    should_break = True
                else:
                    i += 1
                    continue
                numbers = [int(x.strip()) - 1 for x in numbers_str.split(',') if x.strip()]
                numbers = numbers[1:]
                meetingsxBusiness.append(numbers)
                if should_break:
                    i += 1
                    break
            elif line == '];' or line == '};':
                i += 1
                break
            i += 1
        
        # Parse nMeetingsBusiness
        nMeetingsBusiness = []
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if 'nMeetingsBusiness' in line and '[' in line:
                # Extract the array content
                content = line.split('[', 1)[1].rstrip('];')
                nMeetingsBusiness = [0] + [int(x.strip()) for x in content.split(',') if x.strip()]
                i += 1
                break
            i += 1
        
        # Parse forbidden (array of sets)
        forbidden = [[]]
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if 'forbidden' in line and '[' in line:
                # Extract first set if on same line
                content = line.split('[', 1)[1]
                if content.startswith('{'):
                    first_set = content.lstrip('{').rstrip(',}')
                    numbers = [int(x.strip()) for x in first_set.split(',') if x.strip()]
                    numbers = [n for n in numbers if n != 0]  # {0} means empty, keep nonzero
                    forbidden.append(numbers)
            elif line.startswith('{'):
                should_break = False
                if line.endswith('},'):
                    numbers_str = line.strip('{},') 
                elif line.endswith('};') or line.endswith('}];'):
                    numbers_str = line.strip('{};]')
                    should_break = True
                else:
                    i += 1
                    continue
                numbers = [int(x.strip()) for x in numbers_str.split(',') if x.strip()]
                numbers = [n for n in numbers if n != 0]  # {0} means empty, keep nonzero
                forbidden.append(numbers)
                if should_break:
                    i += 1
                    break
            elif line == '];' or line == '};':
                i += 1
                break
            i += 1
        
        # Parse fixed (simple array of integers)
        fixed = []
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if 'fixed' in line and '[' in line:
                # Extract the array content
                content = line.split('[', 1)[1].rstrip('];')
                fixed = [0] + [int(x.strip()) for x in content.split(',') if x.strip()]
                i += 1
                break
            i += 1
        
        # Parse precedences (array of sets)
        precedences = [[]]
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if 'precedences' in line and '[' in line:
                # Extract first set if on same line
                content = line.split('[', 1)[1]
                if content.startswith('{'):
                    first_set = content.lstrip('{').rstrip(',}')
                    if first_set:  # Not empty
                        numbers = [int(x.strip()) for x in first_set.split(',') if x.strip()]
                        precedences.append(numbers)
                    else:
                        precedences.append([])
            elif line.startswith('{'):
                should_break = False
                if line.endswith('},'):
                    numbers_str = line.strip('{},') 
                elif line.endswith('};') or line.endswith('}];'):
                    numbers_str = line.strip('{};]')
                    should_break = True
                else:
                    i += 1
                    continue
                if numbers_str:  # Not empty
                    numbers = [int(x.strip()) for x in numbers_str.split(',') if x.strip()]
                    precedences.append(numbers)
                else:
                    precedences.append([])
                if should_break:
                    i += 1
                    break
            elif line == '];' or line == '};':
                i += 1
                break
            i += 1
        
        return nBusiness, nMeetings, nTables, nTotalSlots, nMorningSlots, requested, meetingsxBusiness, nMeetingsBusiness, forbidden, fixed, precedences

    start_time = time.time()
    nBusiness, nMeetings, nTables, nTotalSlots, nMorningSlots, requested, meetingsxBusiness, nMeetingsBusiness, forbidden, fixed, precedences = read_input()
    input_time = time.time()
    print(f"Input parsing completed in {input_time - start_time:.4f} seconds")

    # print(nBusiness, nMeetings, nTables, nTotalSlots, nMorningSlots)
    # print("Requested:", requested)
    # print("MeetingsxBusiness:", meetingsxBusiness)
    # print("nMeetingsBusiness:", nMeetingsBusiness)
    # print("Forbidden:", forbidden)
    # print("Fixed:", fixed)
    # print("Precedences:", precedences)

    # HARD CONSTRAINTS (19)-(40)

    # x[m][t] = 1 if meeting m is scheduled at time slot t, 0 otherwise
    x = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nMeetings + 1)]

    # y[p][t] = 1 if business p has a meeting at time slot t, 0 otherwise
    y = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    # z[p][t] = 1 if there is at least one meeting from time slot 1 to t for business p, 0 otherwise
    z = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    # h[p][t] = 1 if business p's break gets interrupted at time slot t, 0 otherwise
    h = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]
    
    for m in range(1, nMeetings + 1):
        for t in range(1, nTotalSlots + 1):
            if x[m][t] == 0:
                x[m][t] = variable_size + 1
                variable_size += 1

    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            y[p][t] = variable_size + 1
            variable_size += 1 
            
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            z[p][t] = variable_size + 1
            variable_size += 1
            
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            h[p][t] = variable_size + 1
            variable_size += 1

    # At most one meeting involving the same participant is scheduled at each time slot (19)
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            lits = [x[m][t] for m in meetingsxBusiness[p]]
            if len(lits) > 1:
                atmost_one = CardEnc.atmost(
                    lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size
                )
                variable_size = max(variable_size, atmost_one.nv)
                cnf.extend(atmost_one.clauses)
    # Each meeting happened exactly once (20), (22), (24)
    for m in range(1, nMeetings + 1):
        if requested[m][2] == 3: # No time restriction
            lits = [x[m][t] for t in range(1, nTotalSlots + 1)]
            clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            cnf.extend(clauses)
            variable_size = max(variable_size, clauses.nv)
        elif requested[m][2] == 1: # Morning
            lits = [x[m][t] for t in range(1, nMorningSlots + 1)]
            clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            cnf.extend(clauses)
            variable_size = max(variable_size, clauses.nv)
        else: # Afternoon
            lits = [x[m][t] for t in range(nMorningSlots + 1, nTotalSlots + 1)]
            clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            cnf.extend(clauses)
            variable_size = max(variable_size, clauses.nv)

    # At most nTables meetings can happen at the same time (21)
    for t in range(1, nTotalSlots + 1):
        lits = [x[m][t] for m in range(1, nMeetings + 1)]
        if len(lits) > nTables:
            atmost_tables = CardEnc.atmost(
                lits=lits, bound=nTables, encoding=EncType.seqcounter, top_id=variable_size
            )
            variable_size = max(variable_size, atmost_tables.nv)
            cnf.extend(atmost_tables.clauses)
    # Handle AM/PM restrictions (23), (25)
    for m in range(1, nMeetings + 1):
        if requested[m][2] == 1: # Morning
            for t in range(nMorningSlots + 1, nTotalSlots + 1):
                cnf.append([-x[m][t]])
        elif requested[m][2] == 2: # Afternoon
            for t in range(1, nMorningSlots + 1):
                cnf.append([-x[m][t]])
    # Handle fixed meetings (26)
    for m in range(1, nMeetings + 1):
        if fixed[m] != 0:
            t = fixed[m]
            cnf.append([x[m][t]])
    
    # Handle forbidden time slots (27)
    for p in range(1, nBusiness + 1):
        for t in forbidden[p]:
            cnf.append([-y[p][t]])
    
    # Handle precedence constraints (28)
    for m in range(1, nMeetings + 1):
        for prec in precedences[m]:
            # Add staircase constraints (meeting prec must be scheduled before meeting m)
            sfx = [0 for _ in range(nTotalSlots + 1)]
            sfx[nTotalSlots] = x[prec][nTotalSlots]
            for t in range(nTotalSlots - 1, 0, -1):
                sfx[t] = variable_size + 1
                variable_size += 1
                cnf.append([-x[prec][t], sfx[t]])  # x[prec][t] => sfx[t]
                cnf.append([-sfx[t + 1], sfx[t]])  # sfx[t + 1] => sfx[t]
                cnf.append([x[prec][t], sfx[t + 1], -sfx[t]])  # not x[prec][t] and not sfx[t + 1] => not sfx[t]
            # Strict precedence: prec must be at slot < t (not at t or later)
            for t in range(1, nTotalSlots + 1):
                cnf.append([-x[m][t], -sfx[t]])
            
    # If a meeting is scheduled at time slot t then y[p1][t] and y[p2][t] must be true (29)
    # => x[m][t] -> y[p1][t] and y[p2][t]
    for m in range(1, nMeetings + 1):
        p1 = requested[m][0]
        p2 = requested[m][1]
        for t in range(1, nTotalSlots + 1):
            cnf.append([-x[m][t], y[p1][t]])
            cnf.append([-x[m][t], y[p2][t]])
    # If a time slot is used by business p then one of the meetings involving p must be scheduled at that time slot (30)
    # => y[p][t] -> OR_{m in meetingsxBusiness[p]} x[m][t]
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            lits = [x[m][t] for m in meetingsxBusiness[p]]
            cnf.append([-y[p][t]] + lits)

    # not y[p][1] -> not z[p][1] (31)
    for p in range(1, nBusiness + 1):
        cnf.append([y[p][1], -z[p][1]])
    # (not z[p][t - 1] and not y[p][t]) -> not z[p][t] for t in 2..|T| (32)
    for p in range(1, nBusiness + 1):
        for t in range(2, nTotalSlots + 1):
            cnf.append([z[p][t - 1], y[p][t], -z[p][t]])
    # y[p][t] -> z[p][t] (33)
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            cnf.append([-y[p][t], z[p][t]])
    # z[p][t - 1] -> z[p][t] (34)
    for p in range(1, nBusiness + 1):
        for t in range(2, nTotalSlots + 1):
            cnf.append([-z[p][t - 1], z[p][t]])
    # y[p][t+1] + not h[p][t] + z[p][t] + not y[p][t] <= 3 (35)
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots):
            cnf.append([-y[p][t + 1], h[p][t], -z[p][t], y[p][t]])

    # Maximum number of breaks per participant is floor((|T|-1)/2)
    max_break_count = (nTotalSlots - 1) // 2

    # Build sortedHole[p][j], j in 1..floor((|T|-1)/2) (36)
    sortedHole = [[0 for _ in range(max_break_count + 1)] for _ in range(nBusiness + 1)]

    for p in range(1, nBusiness + 1):
        for j in range(1, max_break_count + 1):
            sortedHole[p][j] = variable_size + 1
            variable_size += 1
    
    for p in range(1, nBusiness + 1):
        end_lits = [h[p][t] for t in range(1, nTotalSlots)]  # endHole over 1..|T|-1

        for j in range(1, max_break_count + 1):
            s = sortedHole[p][j]

            # s -> AtLeast(j, end_lits)
            atleast_j = CardEnc.atleast(
                lits=end_lits, bound=j, encoding=EncType.seqcounter, top_id=variable_size
            )
            variable_size = max(variable_size, atleast_j.nv)
            for c in atleast_j.clauses:
                cnf.append([-s] + c)

            # ¬s -> AtMost(j-1, end_lits)   (equivalently s ∨ clause)
            atmost_jm1 = CardEnc.atmost(
                lits=end_lits, bound=j - 1, encoding=EncType.seqcounter, top_id=variable_size
            )
            variable_size = max(variable_size, atmost_jm1.nv)
            for c in atmost_jm1.clauses:
                cnf.append([s] + c)

            # Optional (redundant but useful): monotonic unary order
            # sortedHole[p][j+1] -> sortedHole[p][j]
            if j < max_break_count:
                cnf.append([-sortedHole[p][j + 1], sortedHole[p][j]])

    # max[j] and min[j] are unary bounds on maximum/minimum breaks among participants.
    max_break = [0 for _ in range(max_break_count + 1)]
    min_break = [0 for _ in range(max_break_count + 1)]
    dif = [0 for _ in range(max_break_count + 1)]

    for j in range(1, max_break_count + 1):
        max_break[j] = variable_size + 1
        variable_size += 1
    for j in range(1, max_break_count + 1):
        min_break[j] = variable_size + 1
        variable_size += 1
    for j in range(1, max_break_count + 1):
        dif[j] = variable_size + 1
        variable_size += 1

    # (37) sortedHole[p][j] -> max[j]
    for p in range(1, nBusiness + 1):
        for j in range(1, max_break_count + 1):
            cnf.append([-sortedHole[p][j], max_break[j]])

    # (38) not sortedHole[p][j] -> not min[j]   (equiv. min[j] -> sortedHole[p][j])
    for p in range(1, nBusiness + 1):
        for j in range(1, max_break_count + 1):
            cnf.append([-min_break[j], sortedHole[p][j]])

    # Monotonicity for max and min
    for j in range(1, max_break_count):
        cnf.append([-max_break[j + 1], max_break[j]])
        cnf.append([-min_break[j + 1], min_break[j]])

    # (39) not min[j] and max[j] -> dif[j]
    for j in range(1, max_break_count + 1):
        cnf.append([min_break[j], -max_break[j], dif[j]])

    # (40) atMost(d, {dif[j]})
    fairness_d = 2
    if max_break_count > 0:
        fairness_lits = [dif[j] for j in range(1, max_break_count + 1)]
        fairness_clauses = CardEnc.atmost(
            lits=fairness_lits,
            bound=min(fairness_d, len(fairness_lits)),
            encoding=EncType.seqcounter,
            top_id=variable_size,
        )
        cnf.extend(fairness_clauses)
        variable_size = max(variable_size, fairness_clauses.nv)

    # Imp1: The number of meetings of a participant p must equal nMeetingsBusiness[p] (43)
    for p in range(1, nBusiness + 1):
        lits = [y[p][t] for t in range(1, nTotalSlots + 1)]
        clauses = CardEnc.equals(lits=lits, bound=nMeetingsBusiness[p], encoding=EncType.seqcounter, top_id=variable_size)
        cnf.extend(clauses)
        variable_size = max(variable_size, clauses.nv)
    
    # Imp2: The number of participants having a meeting in a given time slot is bounded by twice the number of available locations (44)
    for t in range(1, nTotalSlots + 1):
        lits = [y[p][t] for p in range(1, nBusiness + 1)]
        clauses = CardEnc.atmost(lits=lits, bound=2*nTables, encoding=EncType.seqcounter, top_id=variable_size)
        cnf.extend(clauses)
        variable_size = max(variable_size, clauses.nv)
    
    # Add hard clauses 
    for clause in cnf.clauses:
        wcnf.append(clause)  # Default weight is top (hard)

    # SOFT CONSTRAINTS:

    # not sortedHole[p][t] with weight 1 for t from 1 to floor((|T|-1)/2) (41)
    # (Note: This is our objective function)
    for p in range(1, nBusiness + 1):
        for t in range(1, max_break_count + 1):
            wcnf.append([-sortedHole[p][t]], weight=1)

    # (not y[p][t] and h[p][t]) -> not y[p][t + 1] (42)
    # In words, if you are currently in an idle slot and your schedule has already started, you should not have a meeting in the next slot
    # for p in range(1, nBusiness + 1):
    #     for t in range(1, nTotalSlots):
    #         wcnf.append([y[p][t], -h[p][t], -y[p][t + 1]], weight=1)

    constraint_time = time.time()
    print(f"Constraint building completed in {constraint_time - input_time:.4f} seconds")
    print(f"Total variables: {variable_size}")
    print(f"Total clauses: {len(cnf.clauses)}")
    class TimeoutError(Exception):
        pass

    def timeout_handler(signum, frame):
        raise TimeoutError("Solver timeout after 1 hour")

    def solve_maxsat():
        # Set timeout to 1 hour (3600 seconds)
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(3600)
        
        try:
            # Create RC2 solver with the WCNF formula
            solver = RC2(wcnf)
            
            # Compute the optimal solution
            model = solver.compute()
            
            # Get the cost (number of unsatisfied soft clauses)
            if model:
                solution_cost = solver.cost
                print(f"MaxSAT solution found with cost: {solution_cost}")
            else:
                solution_cost = None
            
            # Cancel the alarm if solver finishes before timeout
            signal.alarm(0)
            return model, solution_cost
        except TimeoutError as e:
            signal.alarm(0)
            print(f"TIMEOUT: {str(e)}")
            return None, None

    # Write the WCNF to a file
    wcnf.to_file('maxHS.wcnf')

    # Solve
    solve_start = time.time()
    assignment, cost = solve_maxsat()
    solve_time = time.time()
    print(f"MaxSAT solving completed in {solve_time - solve_start:.4f} seconds")

    # print(assignment)

    end_time = time.time()
    total_time = end_time - start_time

    # Output the schedule based on the assignment

    # if assignment:
    #     for m in range(1, nMeetings + 1):
    #         for t in range(1, nTotalSlots + 1):
    #             if x[m][t] in assignment:
    #                 print(f"Meeting {m} → Time slot {t}")

    print(f"\n{'='*60}")
    print(f"TOTAL RUNTIME: {total_time:.4f} seconds ({total_time:.2f}s)")
    print(f"{'='*60}")

    # Write output to file
    with open(out_path, 'w') as f:
        f.write(f"Input: {base_name}\n")
        f.write(f"{'='*60}\n")
        f.write(f"Total Runtime: {total_time:.4f} seconds\n")
        f.write(f"Input parsing: {input_time - start_time:.4f} seconds\n")
        f.write(f"Constraint building: {constraint_time - input_time:.4f} seconds\n")
        f.write(f"MaxSAT solving: {solve_time - solve_start:.4f} seconds\n")
        f.write(f"{'='*60}\n\n")
        f.write("Total variables: {}\n".format(variable_size))
        f.write("Total clauses: {}\n".format(len(cnf.clauses)))
        f.write("MaxSAT found with cost: {}\n".format(cost if cost is not None else "N/A"))
        if assignment:
            # Checker
            f.write("SCHEDULE:\n")
            for m in range(1, nMeetings + 1):
                for t in range(1, nTotalSlots + 1):
                    if x[m][t] in assignment:
                        f.write(f"Meeting {m} → Time slot {t}\n")
            # Create a mapping of variable numbers to their assigned values
            var_assignment = {abs(var): (var > 0) for var in assignment}
            f.write("Checking hard constraints...\n")

            # Helper: variable truth in assignment
            def is_true(var_id):
                return var_assignment.get(var_id, False)

            # Check hard constraints
            # Each meeting happens exactly once
            for m in range(1, nMeetings + 1):
                count = sum(is_true(x[m][t]) for t in range(1, nTotalSlots + 1))
                assert count == 1, f"Meeting {m} does not happen exactly once (count={count})"
            
            # No more than nTables meetings at the same time
            for t in range(1, nTotalSlots + 1):
                count = sum(is_true(x[m][t]) for m in range(1, nMeetings + 1))
                assert count <= nTables, f"More than {nTables} meetings at time slot {t} (count={count})"
            
            # At most one meeting at moment t for the same business
            for p in range(1, nBusiness + 1):
                for t in range(1, nTotalSlots + 1):
                    count = sum(is_true(x[m][t]) for m in meetingsxBusiness[p])
                    assert count <= 1, f"More than one meeting for business {p} at time slot {t} (count={count})"
            
            # Handle time session
            for m in range(1, nMeetings + 1):
                if requested[m][2] == 3: # No time restriction
                    continue
                elif requested[m][2] == 1: # Morning
                    for t in range(nMorningSlots + 1, nTotalSlots + 1):
                        assert not is_true(x[m][t]), f"Meeting {m} should be in the morning but is scheduled at time slot {t}"
                else: # Afternoon
                    for t in range(1, nMorningSlots + 1):
                        assert not is_true(x[m][t]), f"Meeting {m} should be in the afternoon but is scheduled at time slot {t}"

            # y constraints: exact count and x -> y
            for p in range(1, nBusiness + 1):
                y_count = sum(is_true(y[p][t]) for t in range(1, nTotalSlots + 1))
                assert y_count == nMeetingsBusiness[p], (
                    f"Business {p} has wrong number of occupied slots in y "
                    f"(got={y_count}, expected={nMeetingsBusiness[p]})"
                )
                for t in range(1, nTotalSlots + 1):
                    has_x = any(is_true(x[m][t]) for m in meetingsxBusiness[p])
                    assert is_true(y[p][t]) == has_x, (
                        f"y[{p}][{t}] inconsistent with x variables "
                        f"(y={is_true(y[p][t])}, has_x={has_x})"
                    )

            # y -> z and z monotonicity
            for p in range(1, nBusiness + 1):
                for t in range(1, nTotalSlots + 1):
                    assert (not is_true(y[p][t])) or is_true(z[p][t]), (
                        f"z[{p}][{t}] should be true when y[{p}][{t}] is true"
                    )
                for t in range(2, nTotalSlots + 1):
                    assert (not is_true(z[p][t - 1])) or is_true(z[p][t]), (
                        f"z monotonicity violated for business {p} between t={t-1} and t={t}"
                    )

            # h implication: y[p][t+1] and z[p][t] and not y[p][t] -> h[p][t]
            for p in range(1, nBusiness + 1):
                for t in range(1, nTotalSlots):
                    antecedent = is_true(y[p][t + 1]) and is_true(z[p][t]) and (not is_true(y[p][t]))
                    assert (not antecedent) or is_true(h[p][t]), (
                        f"h[{p}][{t}] should be true by break implication"
                    )

            # Participant load bound per slot
            for t in range(1, nTotalSlots + 1):
                participants_at_t = sum(is_true(y[p][t]) for p in range(1, nBusiness + 1))
                assert participants_at_t <= 2 * nTables, (
                    f"Too many participants at slot {t} (count={participants_at_t}, bound={2*nTables})"
                )

            # (36) sortedHole semantics induced by h variables
            for p in range(1, nBusiness + 1):
                break_count = sum(is_true(h[p][t]) for t in range(1, nTotalSlots))
                for j in range(1, max_break_count + 1):
                    expected_sorted = (break_count >= j)
                    assert is_true(sortedHole[p][j]) == expected_sorted, (
                        f"sortedHole[{p}][{j}] inconsistent with break count "
                        f"(break_count={break_count})"
                    )

            # (37) sortedHole[p][j] -> max[j]
            for p in range(1, nBusiness + 1):
                for j in range(1, max_break_count + 1):
                    assert (not is_true(sortedHole[p][j])) or is_true(max_break[j]), (
                        f"(37) violated at p={p}, j={j}"
                    )

            # (38) not sortedHole[p][j] -> not min[j]
            for p in range(1, nBusiness + 1):
                for j in range(1, max_break_count + 1):
                    assert is_true(sortedHole[p][j]) or (not is_true(min_break[j])), (
                        f"(38) violated at p={p}, j={j}"
                    )

            # Unary monotonicity for max and min
            for j in range(1, max_break_count):
                assert (not is_true(max_break[j + 1])) or is_true(max_break[j]), (
                    f"max unary monotonicity violated at j={j}"
                )
                assert (not is_true(min_break[j + 1])) or is_true(min_break[j]), (
                    f"min unary monotonicity violated at j={j}"
                )

            # (39) not min[j] and max[j] -> dif[j]
            for j in range(1, max_break_count + 1):
                antecedent = (not is_true(min_break[j])) and is_true(max_break[j])
                assert (not antecedent) or is_true(dif[j]), f"(39) violated at j={j}"

            # (40) atMost(d, {dif[j]})
            dif_count = sum(is_true(dif[j]) for j in range(1, max_break_count + 1))
            assert dif_count <= fairness_d, (
                f"(40) violated: dif_count={dif_count}, fairness_d={fairness_d}"
            )
            
            # Check forbidden time slots
            for p in range(1, nBusiness + 1):
                for t in forbidden[p]:
                    assert not is_true(y[p][t]), f"Business {p} has a meeting at forbidden time slot {t}"
            
            # Check fixed meetings
            for m in range(1, nMeetings + 1):
                if fixed[m] != 0:
                    t = fixed[m]
                    assert is_true(x[m][t]), f"Meeting {m} should be scheduled at time slot {t} but is not"
            
            # Check precedence constraints
            for m in range(1, nMeetings + 1):
                for prec in precedences[m]:
                    prec_time = None
                    m_time = None
                    for t in range(1, nTotalSlots + 1):
                        if is_true(x[prec][t]):
                            prec_time = t
                        if is_true(x[m][t]):
                            m_time = t
                    assert prec_time is not None and m_time is not None, f"Precedence constraint between meeting {prec} and {m} is not satisfied (prec_time={prec_time}, m_time={m_time})"
                    assert prec_time < m_time, f"Meeting {prec} should be scheduled before meeting {m} (prec_time={prec_time}, m_time={m_time})"
            f.write("All hard constraints are satisfied\n")
        else:
            f.write("NO SOLUTION FOUND\n")
    
    print(f"Output written to: {out_path}")
