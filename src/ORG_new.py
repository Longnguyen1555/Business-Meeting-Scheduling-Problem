import sys
import argparse
from pysat.formula import CNF, WCNF
from pysat.card import CardEnc, EncType
from math import inf, sqrt
import subprocess
import shlex
import time
import os
import threading
import tempfile
from pathlib import Path

from Excel_Results import FORMULA_SCOPE, RUNTIME_SCOPE, safe_workbook_name, write_instance_workbook
from Main import (
    collect_instances,
    experiment_metadata,
    instance_result_metadata,
    write_detailed_csv,
)
from MaxSAT_Solver import (
    UWRMAXSAT_NOT_FOUND_MESSAGE,
    executable_sha256,
    resolve_uwrmaxsat_binary,
)
from B2B_Instance import (
    compute_participant_idle_bounds,
    interpolate_participant_idle_caps,
    read_instance as read_shared_instance,
)

try:
    import psutil
except ImportError:
    # Chương trình vẫn chạy; cột memory sẽ để trống nếu chưa cài psutil.
    psutil = None


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            'Solve B2B instances with the paper-style ORG MaxSAT encoding. '
            'Paper-style ORG encoding for IR, IR-IM-IS, or IR-IM-ISQ, '
            'with optional participant idle caps.'
        )
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument('--instance', help='single .dzn instance')
    input_group.add_argument('--data-dir', help='SHA-256-deduplicated .dzn directory')
    input_group.add_argument('--manifest', help='canonical instances_manifest.csv')
    parser.add_argument(
        '--family',
        choices=['all', 'original', 'forbidden', 'fixed', 'precedence'],
        default='all',
    )
    parser.add_argument(
        '--keep-path-aliases',
        action='store_true',
        help=(
            'with --data-dir, run every selected .dzn path even when multiple '
            'paths have identical SHA-256 content'
        ),
    )
    parser.add_argument('--timeout', type=float, default=7200.0)
    parser.add_argument('--uwrmaxsat-bin')
    parser.add_argument('--uwrmaxsat-sha256')
    parser.add_argument(
        '--objective-mode',
        choices=['ir', 'ir_im_is', 'ir_im_isq'],
        default='ir',
    )
    parser.add_argument(
        '--participant-idle-cap-rule',
        choices=['none', 'interpolated_bounds'],
        default='none',
    )
    parser.add_argument('--participant-idle-cap-alpha', default='1/2')
    parser.add_argument('--csv')
    parser.add_argument('--excel-dir')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.keep_path_aliases and args.data_dir is None:
        parser.error('--keep-path-aliases requires --data-dir')
    if args.participant_idle_cap_rule != 'none' and args.objective_mode == 'ir':
        parser.error('participant idle caps apply only to IR-IM-IS/ISQ')
    try:
        interpolate_participant_idle_caps((0,), (1,), args.participant_idle_cap_alpha)
    except (ValueError, ZeroDivisionError) as exc:
        parser.error(str(exc))
    return args


ARGS = parse_args()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

OUTPUT_DIR = os.path.join(PROJECT_DIR, 'output')

# Một bảng CSV tổng hợp cho toàn bộ instance, tương tự detailed CSV trong Main.
CSV_OUTPUT_FILE = ARGS.csv or os.path.join(
    OUTPUT_DIR,
    'ORG_new_baseline_results.csv',
)
EXCEL_OUTPUT_DIR = Path(ARGS.excel_dir or (Path(CSV_OUTPUT_FILE).parent / 'excel_org'))

# Giống Main.py: lấy peak RSS của tiến trình chạy instance và toàn bộ child process.
MEMORY_SAMPLE_INTERVAL_S = 0.05
MEMORY_METRIC = 'peak_process_tree_rss_mb'
ORG_IMPLIED_PACKAGE_CODE = 'OBIC12P'
ORG_IMPLIED_PACKAGE_NAME = 'OldBestIC12+'
ORG_ENCODING_VARIANT = 'org_old_best_ic12plus'
ORG_OBJECTIVE_METADATA = {
    'ir': ('IRP', 'IdleRangePstar', 'internal_idle_slot_range_pstar'),
    'ir_im_is': (
        'IRIMIS', 'IdleRangeThenMaximumIdleThenIdleSum',
        'lexicographic_idle_range_maximum_idle_then_idle_sum',
    ),
    'ir_im_isq': (
        'IRIMISQ', 'IdleRangeThenMaximumIdleThenSquaredIdleSum',
        'lexicographic_idle_range_maximum_idle_then_squared_idle_sum',
    ),
}
ORG_OBJECTIVE_CODE, ORG_OBJECTIVE_FACTOR, ORG_OBJECTIVE_NAME = (
    ORG_OBJECTIVE_METADATA[ARGS.objective_mode]
)
ORG_CONFIGURATION_LABEL = (
    f'ORG-F-PW-DE-PSC-{ORG_OBJECTIVE_CODE}-UW-{ORG_IMPLIED_PACKAGE_CODE}'
)
ORG_CONFIGURATION_ID = (
    'baseline1__model-org_old_best_maxsat__m-full__p-pairwise__'
    'g-direct__b-per_slot_cardinality__o-idle_range_pstar__'
    's-uwrmaxsat__i-old_best_ic12plus__fairness-none'
    if ARGS.objective_mode == 'ir'
    else (
        'baseline1__model-org_old_best_maxsat__m-full__p-pairwise__'
        'g-direct__b-per_slot_cardinality__'
        f'o-{ARGS.objective_mode}__s-uwrmaxsat__i-old_best_ic12plus__'
        f'cap-{ARGS.participant_idle_cap_rule}__'
        f'alpha-{ARGS.participant_idle_cap_alpha.replace("/", "of")}'
    )
)

os.makedirs(OUTPUT_DIR, exist_ok=True)
EXCEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _process_tree_rss_bytes(pid):
    """RSS hiện tại của process pid và toàn bộ child process đệ quy."""
    if psutil is None:
        return None

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return None

    total = 0
    found = False
    for process in processes:
        try:
            total += process.memory_info().rss
            found = True
        except (psutil.Error, OSError):
            continue

    return total if found else None


class ProcessTreeMemorySampler:
    """Lấy mẫu peak process-tree RSS trong lúc xử lý một instance."""

    def __init__(self, pid, interval_s=MEMORY_SAMPLE_INTERVAL_S):
        self.pid = pid
        self.interval_s = interval_s
        self.peak_rss_bytes = None
        self._stop_event = threading.Event()
        self._thread = None

    def _sample_once(self):
        rss = _process_tree_rss_bytes(self.pid)
        if rss is not None:
            self.peak_rss_bytes = max(self.peak_rss_bytes or 0, rss)

    def _run(self):
        while not self._stop_event.wait(self.interval_s):
            self._sample_once()

    def start(self):
        self._sample_once()
        if psutil is not None:
            self._thread = threading.Thread(
                target=self._run,
                name='org-new-memory-sampler',
                daemon=True,
            )
            self._thread.start()
        return self

    def stop_mb(self):
        self._sample_once()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, 2 * self.interval_s))
        self._sample_once()
        if self.peak_rss_bytes is None:
            return None
        return round(self.peak_rss_bytes / (1024 * 1024), 3)


def serialize_list(values):
    return ','.join(str(value) for value in values)


def serialize_assignment(schedule):
    return ','.join(
        f'M{meeting}:T{slot}'
        for meeting, slot in schedule
    )


def serialize_schedule(meetings_per_slot):
    parts = []
    for slot, meetings in enumerate(meetings_per_slot, start=1):
        meeting_text = ' '.join(f'M{meeting}' for meeting in meetings)
        parts.append(f'{slot}:{meeting_text}')
    return ' | '.join(parts)


def status_to_sat_result(status):
    if status == 'OPTIMAL':
        return 'SAT'
    if status == 'UNSAT':
        return 'UNSAT'
    if status == 'TIMEOUT':
        return 'TIMEOUT'
    return 'ERROR'


csv_results = []

instance_specs = collect_instances(
    ARGS.instance,
    ARGS.data_dir,
    ARGS.manifest,
    ARGS.family,
    ARGS.keep_path_aliases,
)
experiment = experiment_metadata(ARGS, None, runner_path=__file__)

UWRMAXSAT_BINARY = resolve_uwrmaxsat_binary(ARGS.uwrmaxsat_bin)
if UWRMAXSAT_BINARY is None:
    raise SystemExit(f'ERROR: {UWRMAXSAT_NOT_FOUND_MESSAGE}')
UWRMAXSAT_BINARY_SHA256 = executable_sha256(UWRMAXSAT_BINARY)
expected_uwr_sha256 = (ARGS.uwrmaxsat_sha256 or '').strip().lower()
if expected_uwr_sha256 and (
    len(expected_uwr_sha256) != 64
    or any(char not in '0123456789abcdef' for char in expected_uwr_sha256)
):
    raise SystemExit(
        'ERROR: --uwrmaxsat-sha256 must be a 64-character hex digest'
    )
if expected_uwr_sha256 and expected_uwr_sha256 != UWRMAXSAT_BINARY_SHA256:
    raise SystemExit(
        'ERROR: UWrMaxSAT executable SHA-256 mismatch: '
        f'expected {expected_uwr_sha256}, got {UWRMAXSAT_BINARY_SHA256}'
    )

print(
    f'Found {len(instance_specs)} selected input paths '
    f'(content_deduplicated={not ARGS.keep_path_aliases})'
)

test_counter = 0

for instance_spec in instance_specs:
    test_counter += 1

    input_file = str(instance_spec.path)
    base_name = os.path.basename(input_file)

    print(f"\n{'=' * 60}")
    print(f"Processing: {base_name}")
    print(f"Test number: {test_counter}")
    print(f"{'=' * 60}")

    in_path = input_file

    # Bắt đầu đo memory riêng cho instance hiện tại. Giá trị là peak RSS tuyệt đối
    # của process ORG_new cộng toàn bộ child process (ví dụ UWrMaxSAT).
    memory_sampler = ProcessTreeMemorySampler(os.getpid()).start()

    # Không lọc tên file.
    # Tất cả file .dzn đều được chạy.

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

    def pairwise_AMO(lits):
        clauses = []
        for i in range(len(lits)):
            for j in range(i + 1, len(lits)):
                clauses.append([-lits[i], -lits[j]])
        return clauses
    
    def ALO(lits):
        clauses = [lits]
        return clauses

    # Exactly 1
    def commander_EO(lits):
        global variable_size
        sz = len(lits)
        if sz == 0:
            return
        if sz == 1:
            cnf.append([lits[0]])
            return
        # Base case: small enough to encode directly with pairwise AMO + ALO
        if sz <= 4:
            cnf.extend(pairwise_AMO(lits))
            cnf.extend(ALO(lits))
            return

        group_size = max(2, int(sqrt(sz)) + (1 if int(sqrt(sz)) ** 2 < sz else 0))
        commanders = []
        for i in range(0, sz, group_size):
            group = lits[i:min(sz, i + group_size)]
            c = variable_size + 1
            variable_size += 1
            commanders.append(c)
            # AMO within the group
            cnf.extend(pairwise_AMO(group))
            # c_i -> ALO(group)
            cnf.append([-c] + group)
            # X_j -> c_i:
            for x in group:
                cnf.append([-x, c])

        # Exactly one commander is true
        cnf.extend(pairwise_AMO(commanders))
        cnf.extend(ALO(commanders))

    def add_comparator(left, right, high, low):
        """Exact comparator used by the cardinality/sorting networks."""
        # high <-> (left OR right)
        cnf.append([-left, high])
        cnf.append([-right, high])
        cnf.append([left, right, -high])
        # low <-> (left AND right)
        cnf.append([left, -low])
        cnf.append([right, -low])
        cnf.append([-left, -right, low])

    network_false_var = 0

    def _network_false_literal():
        """Return a shared Boolean constant fixed to false for network padding."""
        global variable_size, network_false_var
        if network_false_var == 0:
            network_false_var = variable_size + 1
            variable_size += 1
            cnf.append([-network_false_var])
        return network_false_var

    def sort_descending(lits):
        """Sort Boolean literals with a Batcher odd-even cardinality network.

        Output k-1 is true exactly when at least k input literals are true.  This
        is the unary/sorted representation used by the paper for sort(.,.) and
        for the cardinality-network implementation of Constraints (44)-(45).
        """
        global variable_size

        if not lits:
            return []
        if len(lits) == 1:
            return list(lits)

        # Batcher's odd-even merge network is defined for powers of two. False
        # padding preserves all threshold values of the original input list.
        size = 1
        while size < len(lits):
            size *= 2
        wires = list(lits)
        if len(wires) < size:
            wires.extend([_network_false_literal()] * (size - len(wires)))

        def compare(i, j):
            global variable_size
            high = variable_size + 1
            low = variable_size + 2
            variable_size = low
            add_comparator(wires[i], wires[j], high, low)
            wires[i], wires[j] = high, low

        def odd_even_merge(lo, length, stride):
            step = stride * 2
            if step < length:
                odd_even_merge(lo, length, step)
                odd_even_merge(lo + stride, length, step)
                for index in range(lo + stride, lo + length - stride, step):
                    compare(index, index + stride)
            else:
                compare(lo, lo + stride)

        def odd_even_merge_sort(lo, length):
            if length > 1:
                half = length // 2
                odd_even_merge_sort(lo, half)
                odd_even_merge_sort(lo + half, half)
                odd_even_merge(lo, length, 1)

        odd_even_merge_sort(0, size)
        return wires[:len(lits)]

    def build_meeting_clusters():
        """Greedy partition Π used by the paper's Constraints (46)-(47)."""
        unassigned = set(range(1, nMeetings + 1))
        clusters = []
        while unassigned:
            best = []
            for p in range(1, nBusiness + 1):
                candidate = sorted(unassigned.intersection(meetingsxBusiness[p]))
                if len(candidate) > len(best):
                    best = candidate
            if not best:
                best = [min(unassigned)]
            clusters.append(best)
            unassigned.difference_update(best)
        return clusters


    start_time = time.time()
    nBusiness, nMeetings, nTables, nTotalSlots, nMorningSlots, requested, meetingsxBusiness, nMeetingsBusiness, forbidden, fixed, precedences = read_input()
    shared_instance = read_shared_instance(instance_spec.path)
    idle_lower_zero, idle_upper_zero, idle_bounds_feasible = (
        compute_participant_idle_bounds(shared_instance)
    )
    idle_caps_zero = (
        interpolate_participant_idle_caps(
            idle_lower_zero,
            idle_upper_zero,
            ARGS.participant_idle_cap_alpha,
        )
        if ARGS.participant_idle_cap_rule == 'interpolated_bounds'
        else idle_upper_zero
    )
    participant_idle_lower_bounds = [0, *idle_lower_zero]
    participant_idle_upper_bounds = [0, *idle_upper_zero]
    participant_idle_caps = [0, *idle_caps_zero]
    objective_participants = [
        p for p in range(1, nBusiness + 1)
        if nMeetingsBusiness[p] >= 2
    ]
    # The range of an empty or singleton P* is zero, so no break-objective
    # variables are required in those degenerate cases.
    objective_encoding_participants = (
        objective_participants
        if ARGS.objective_mode != 'ir' or len(objective_participants) >= 2
        else []
    )
    input_time = time.time()
    print(f"Input parsing completed in {input_time - start_time:.4f} seconds")

    # print(nBusiness, nMeetings, nTables, nTotalSlots, nMorningSlots)
    # print("Requested:", requested)
    # print("MeetingsxBusiness:", meetingsxBusiness)
    # print("nMeetingsBusiness:", nMeetingsBusiness)
    # print("Forbidden:", forbidden)
    # print("Fixed:", fixed)
    # print("Precedences:", precedences)

    # HARD CONSTRAINTS: keep the original meeting-centered encoding.
    # Only the break-range semantics and P* objective are replaced below.

    # x[m][t] = 1 iff meeting m is scheduled at time slot t.
    x = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nMeetings + 1)]

    # y[p][t] = 1 iff participant p has a meeting at time slot t.
    y = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    # prefix[p][t] = 1 iff p has a meeting at or before slot t.
    prefix = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    # suffix[p][t] = 1 iff p has a meeting at or after slot t.
    suffix = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    # gap_slot[p][t] = 1 iff slot t is empty and lies strictly between
    # p's first and last meetings. Only internal slots 2..T-1 can be gaps.
    gap_slot = [[0 for _ in range(nTotalSlots + 1)] for _ in range(nBusiness + 1)]

    for m in range(1, nMeetings + 1):
        for t in range(1, nTotalSlots + 1):
            x[m][t] = variable_size + 1
            variable_size += 1

    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            y[p][t] = variable_size + 1
            variable_size += 1

    for p in objective_encoding_participants:
        for t in range(1, nTotalSlots + 1):
            prefix[p][t] = variable_size + 1
            variable_size += 1
            suffix[p][t] = variable_size + 1
            variable_size += 1

        for t in range(2, nTotalSlots):
            gap_slot[p][t] = variable_size + 1
            variable_size += 1

    # At most one meeting involving the same participant is scheduled at each time slot (19)
    for p in range(1, nBusiness + 1):
        for t in range(1, nTotalSlots + 1):
            lits = [x[m][t] for m in meetingsxBusiness[p]]
            if len(lits) > 1:
                # atmost_one = CardEnc.atmost(
                #     lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size
                # )
                atmost_one = pairwise_AMO(lits)
                cnf.extend(atmost_one)

    # Each meeting happened exactly once (20), (22), (24)
    for m in range(1, nMeetings + 1):
        if requested[m][2] == 3: # No time restriction
            lits = [x[m][t] for t in range(1, nTotalSlots + 1)]
            # clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            commander_EO(lits)
        elif requested[m][2] == 1: # Morning
            lits = [x[m][t] for t in range(1, nMorningSlots + 1)]
            # clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            commander_EO(lits)
        else: # Afternoon
            lits = [x[m][t] for t in range(nMorningSlots + 1, nTotalSlots + 1)]
            # clauses = CardEnc.equals(lits=lits, bound=1, encoding=EncType.seqcounter, top_id=variable_size)
            commander_EO(lits)

    # Further improvement from the paper, Constraints (46)-(47): replace
    # meeting-level capacity (21) by a sequential-counter capacity constraint
    # over greedily constructed clusters of mutually exclusive meetings.
    meeting_clusters = build_meeting_clusters()
    cluster_active = [
        [0 for _ in range(nTotalSlots + 1)]
        for _ in range(len(meeting_clusters))
    ]

    for c in range(len(meeting_clusters)):
        for t in range(1, nTotalSlots + 1):
            cluster_active[c][t] = variable_size + 1
            variable_size += 1

    for t in range(1, nTotalSlots + 1):
        active_lits = []
        for c, meetings in enumerate(meeting_clusters):
            active = cluster_active[c][t]
            active_lits.append(active)
            # Constraint (46): schedule[m,t] -> clusterActive[c,t].
            for m in meetings:
                cnf.append([-x[m][t], active])

        # Constraint (47), encoded by a sequential counter as specified in §4.6.
        if len(active_lits) > nTables:
            atmost_tables = CardEnc.atmost(
                lits=active_lits,
                bound=nTables,
                encoding=EncType.seqcounter,
                top_id=variable_size,
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
    
    # Forbidden time slots (27), directly over schedule variables as in
    # the paper: every meeting of p is forbidden at each slot in forb(p).
    for p in range(1, nBusiness + 1):
        for t in forbidden[p]:
            for m in meetingsxBusiness[p]:
                cnf.append([-x[m][t]])

    # Traditional pairwise precedence encoding (28) from the paper:
    # schedule[prec,j0] -> not schedule[m,j] for every j0 >= j.
    for m in range(1, nMeetings + 1):
        for prec in precedences[m]:
            for t in range(1, nTotalSlots + 1):
                for prec_t in range(t, nTotalSlots + 1):
                    cnf.append([-x[prec][prec_t], -x[m][t]])
            
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

    # ------------------------------------------------------------------
    # NEW BREAK SEMANTICS
    # total_gap_slots[p] = number of empty slots strictly between the
    # first and last meetings of participant p.
    # ------------------------------------------------------------------

    # prefix[p][t] <-> OR(y[p][1], ..., y[p][t]).
    for p in objective_encoding_participants:
        if nTotalSlots >= 1:
            cnf.append([y[p][1], -prefix[p][1]])
            cnf.append([-y[p][1], prefix[p][1]])
        for t in range(2, nTotalSlots + 1):
            cnf.append([-y[p][t], prefix[p][t]])
            cnf.append([-prefix[p][t - 1], prefix[p][t]])
            cnf.append([y[p][t], prefix[p][t - 1], -prefix[p][t]])

    # suffix[p][t] <-> OR(y[p][t], ..., y[p][T]).
    for p in objective_encoding_participants:
        if nTotalSlots >= 1:
            cnf.append([y[p][nTotalSlots], -suffix[p][nTotalSlots]])
            cnf.append([-y[p][nTotalSlots], suffix[p][nTotalSlots]])
        for t in range(nTotalSlots - 1, 0, -1):
            cnf.append([-y[p][t], suffix[p][t]])
            cnf.append([-suffix[p][t + 1], suffix[p][t]])
            cnf.append([y[p][t], suffix[p][t + 1], -suffix[p][t]])

    # gap_slot[p][t] <-> prefix[p][t-1] AND not y[p][t]
    #                                  AND suffix[p][t+1].
    for p in objective_encoding_participants:
        for t in range(2, nTotalSlots):
            g = gap_slot[p][t]
            cnf.append([-g, prefix[p][t - 1]])
            cnf.append([-g, -y[p][t]])
            cnf.append([-g, suffix[p][t + 1]])
            cnf.append([-prefix[p][t - 1], y[p][t], -suffix[p][t + 1], g])

    # Unary representation sortedGap[p][j]: participant p has at least j
    # internal gap slots. The largest possible value for p is T-|M_p|
    # when p has at least two meetings; otherwise it is zero.
    participant_gap_upper = [0 for _ in range(nBusiness + 1)]
    for p in range(1, nBusiness + 1):
        if nMeetingsBusiness[p] >= 2:
            participant_gap_upper[p] = max(0, nTotalSlots - nMeetingsBusiness[p])

    max_gap_slots = max(
        (participant_gap_upper[p] for p in objective_encoding_participants),
        default=0,
    )
    sortedGap = [[0 for _ in range(max_gap_slots + 1)] for _ in range(nBusiness + 1)]

    for p in objective_encoding_participants:
        for j in range(1, max_gap_slots + 1):
            sortedGap[p][j] = variable_size + 1
            variable_size += 1

    for p in objective_encoding_participants:
        gap_lits = [gap_slot[p][t] for t in range(2, nTotalSlots)]
        upper = min(participant_gap_upper[p], len(gap_lits))

        if upper > 0:
            unary_outputs = sort_descending(gap_lits)
            for j in range(1, upper + 1):
                s = sortedGap[p][j]
                output = unary_outputs[j - 1]
                cnf.append([-s, output])
                cnf.append([-output, s])

        # Thresholds above the participant-specific upper bound are false.
        for j in range(upper + 1, max_gap_slots + 1):
            cnf.append([-sortedGap[p][j]])

    participant_idle_cap_clause_count = 0
    if ARGS.participant_idle_cap_rule == 'interpolated_bounds':
        for p in objective_participants:
            cap = participant_idle_caps[p]
            if cap < max_gap_slots:
                cnf.append([-sortedGap[p][cap + 1]])
                participant_idle_cap_clause_count += 1

    # Exact unary maximum, minimum, and their difference. For each j:
    # maxGap[j] = OR_{p in P*} sortedGap[p][j]
    # minGap[j] = AND_{p in P*} sortedGap[p][j]
    # difGap[j] = maxGap[j] AND not minGap[j]
    # Therefore sum_j difGap[j] = max_{p in P*} G_p - min_{p in P*} G_p.
    maxGap = [0 for _ in range(max_gap_slots + 1)]
    minGap = [0 for _ in range(max_gap_slots + 1)]
    difGap = [0 for _ in range(max_gap_slots + 1)]

    for j in range(1, max_gap_slots + 1):
        maxGap[j] = variable_size + 1
        variable_size += 1
        minGap[j] = variable_size + 1
        variable_size += 1
        difGap[j] = variable_size + 1
        variable_size += 1

        threshold_lits = [
            sortedGap[p][j] for p in objective_encoding_participants
        ]
        if threshold_lits:
            # maxGap[j] <-> OR(threshold_lits)
            for lit in threshold_lits:
                cnf.append([-lit, maxGap[j]])
            cnf.append([-maxGap[j]] + threshold_lits)

            # minGap[j] <-> AND(threshold_lits)
            for lit in threshold_lits:
                cnf.append([-minGap[j], lit])
            cnf.append([-lit for lit in threshold_lits] + [minGap[j]])
        else:
            cnf.append([-maxGap[j]])
            cnf.append([minGap[j]])

        # difGap[j] <-> maxGap[j] AND not minGap[j]
        cnf.append([-difGap[j], maxGap[j]])
        cnf.append([-difGap[j], -minGap[j]])
        cnf.append([-maxGap[j], minGap[j], difGap[j]])

    # Helpful unary monotonicity (already implied, but propagation-friendly).
    for j in range(1, max_gap_slots):
        cnf.append([-maxGap[j + 1], maxGap[j]])
        cnf.append([-minGap[j + 1], minGap[j]])

    # No hard objective cap is generated. The difGap literals are objective
    # literals only; all feasible schedules remain admissible.

    # Imp1: The number of meetings of a participant p must equal nMeetingsBusiness[p] (43)
    for p in range(1, nBusiness + 1):
        lits = [y[p][t] for t in range(1, nTotalSlots + 1)]
        clauses = CardEnc.equals(lits=lits, bound=nMeetingsBusiness[p], encoding=EncType.cardnetwrk, top_id=variable_size)
        cnf.extend(clauses)
        variable_size = max(variable_size, clauses.nv)
    
    # Imp2 (44) and the even-participant strengthening (45).  The paper
    # encodes (44) with a cardinality network and applies (45) directly to its
    # sorted unary outputs.  output[k-1] means that at least k participants are
    # active in this slot.
    for t in range(1, nTotalSlots + 1):
        lits = [y[p][t] for p in range(1, nBusiness + 1)]
        if len(lits) > 2 * nTables:
            outputs = sort_descending(lits)

            # atMost(2*nTables): forbid count >= 2*nTables + 1.
            cnf.append([-outputs[2 * nTables]])

            # Constraint (45): o_i -> o_(i+1) for odd one-based i.
            # Together with sorted-output monotonicity this forces an even count.
            for i in range(0, 2 * nTables, 2):
                cnf.append([-outputs[i], outputs[i + 1]])

    
    # Add hard clauses 
    for clause in cnf.clauses:
        wcnf.append(clause)  # Default weight is top (hard)

    # SOFT CONSTRAINTS: exact one-shot lexicographic scalarization.
    effective_upper = {
        p: (
            participant_idle_caps[p]
            if ARGS.participant_idle_cap_rule == 'interpolated_bounds'
            else participant_gap_upper[p]
        )
        for p in objective_participants
    }
    idle_sum_upper = sum(effective_upper.values())
    squared_idle_upper = sum(value * value for value in effective_upper.values())
    final_upper = (
        squared_idle_upper if ARGS.objective_mode == 'ir_im_isq'
        else idle_sum_upper
    )
    maximum_upper = max(effective_upper.values(), default=0)
    maximum_weight = final_upper + 1
    range_weight = (maximum_upper + 1) * (final_upper + 1)
    objective_tier_weights = (
        (1,) if ARGS.objective_mode == 'ir'
        else (range_weight, maximum_weight, 1)
    )
    soft_clause_weights = []
    for j in range(1, max_gap_slots + 1):
        weight = 1 if ARGS.objective_mode == 'ir' else range_weight
        wcnf.append([-difGap[j]], weight=weight)
        soft_clause_weights.append(weight)
    if ARGS.objective_mode in {'ir_im_is', 'ir_im_isq'}:
        for j in range(1, max_gap_slots + 1):
            wcnf.append([-maxGap[j]], weight=maximum_weight)
            soft_clause_weights.append(maximum_weight)
        for p in objective_participants:
            for j in range(1, max_gap_slots + 1):
                weight = 2 * j - 1 if ARGS.objective_mode == 'ir_im_isq' else 1
                wcnf.append([-sortedGap[p][j]], weight=weight)
                soft_clause_weights.append(weight)

    constraint_time = time.time()
    print(f"Constraint building completed in {constraint_time - input_time:.4f} seconds")
    print(f"Total variables: {variable_size}")
    print(f"Total hard clauses: {len(cnf.clauses)}")
    print(f"Total soft clauses: {len(soft_clause_weights)}")
    print(
        f'Participant idle cap: {ARGS.participant_idle_cap_rule} '
        f'(added clauses: {participant_idle_cap_clause_count})'
    )

    def parse_uwr_output(output):
        model = []
        solution_cost = None
        status = None
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith('s '):
                status = line[2:].strip()
            elif line.startswith('o '):
                try:
                    solution_cost = int(line[2:].strip())
                except ValueError:
                    pass
            elif line.startswith('v '):
                for token in line[2:].split():
                    try:
                        literal = int(token)
                    except ValueError:
                        continue
                    if literal != 0:
                        model.append(literal)
        return status, solution_cost, model

    def solve_maxsat(wcnf_path):
        command = [str(UWRMAXSAT_BINARY), '-m', str(wcnf_path)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=ARGS.timeout,
                check=False,
                start_new_session=(os.name != 'nt'),
            )
            output = '\n'.join(
                part for part in (result.stdout, result.stderr) if part
            )
            status, solution_cost, model = parse_uwr_output(output)
            normalized_status = (status or '').upper()
            if normalized_status in {'OPTIMUM FOUND', 'OPTIMAL', 'OPTIMUM'} and model:
                print(f"UWrMaxSAT optimum scalar cost: {solution_cost}")
                return model, solution_cost, 'UWrMaxSAT', 'OPTIMAL', command
            if normalized_status in {'UNSAT', 'UNSATISFIABLE'}:
                return None, None, 'UWrMaxSAT', 'UNSAT', command
            print(f"UWrMaxSAT status: {status}")
            return None, solution_cost, 'UWrMaxSAT', 'ERROR', command
        except subprocess.TimeoutExpired as exc:
            partial_parts = []
            for part in (exc.stdout, exc.stderr):
                if isinstance(part, bytes):
                    part = part.decode(errors='replace')
                if part:
                    partial_parts.append(part)
            _, solution_cost, model = parse_uwr_output('\n'.join(partial_parts))
            print(f"TIMEOUT: UWrMaxSAT timeout after {ARGS.timeout} seconds")
            return (
                model or None,
                solution_cost,
                'UWrMaxSAT',
                'TIMEOUT',
                command,
            )

    with tempfile.NamedTemporaryFile(
        prefix=f'org_{Path(base_name).stem}_',
        suffix='.wcnf',
        delete=False,
    ) as temp_wcnf:
        wcnf_path = Path(temp_wcnf.name)
    wcnf.to_file(str(wcnf_path))

    # Solve
    solve_start = time.time()
    try:
        (
            assignment,
            solver_cost,
            solver_used,
            solve_status,
            solver_command,
        ) = solve_maxsat(wcnf_path)
    finally:
        wcnf_path.unlink(missing_ok=True)
    solver_finished = time.time()

    # Independently recompute the new objective from the decoded schedule.
    participant_gap_slots = []
    idle_range_pstar = None
    maximum_internal_idle_slots = None
    squared_internal_idle_slots = None
    total_internal_idle_slots = None
    objective_vector = None
    lexicographic_scalar_cost = None
    if assignment:
        positive_assignment = {lit for lit in assignment if lit > 0}
        for p in range(1, nBusiness + 1):
            busy_slots = [
                t for t in range(1, nTotalSlots + 1)
                if y[p][t] in positive_assignment
            ]
            if len(busy_slots) <= 1:
                participant_gap_slots.append(0)
            else:
                first_slot = min(busy_slots)
                last_slot = max(busy_slots)
                busy_set = set(busy_slots)
                participant_gap_slots.append(sum(
                    1 for t in range(first_slot + 1, last_slot)
                    if t not in busy_set
                ))

        objective_values = [
            participant_gap_slots[p - 1] for p in objective_participants
        ]
        idle_range_pstar = (
            max(objective_values) - min(objective_values)
            if len(objective_values) >= 2
            else 0
        )
        maximum_internal_idle_slots = max(objective_values, default=0)
        total_internal_idle_slots = sum(participant_gap_slots)
        squared_internal_idle_slots = sum(value * value for value in objective_values)
        objective_vector = {
            'ir': (idle_range_pstar,),
            'ir_im_is': (
                idle_range_pstar,
                maximum_internal_idle_slots,
                total_internal_idle_slots,
            ),
            'ir_im_isq': (
                idle_range_pstar,
                maximum_internal_idle_slots,
                squared_internal_idle_slots,
            ),
        }[ARGS.objective_mode]
        lexicographic_scalar_cost = sum(
            weight * value
            for weight, value in zip(objective_tier_weights, objective_vector)
        )
        if solver_cost is not None:
            assert solver_cost == lexicographic_scalar_cost, (
                f"Objective mismatch: solver_cost={solver_cost}, "
                f"recomputed_scalar={lexicographic_scalar_cost}"
            )
    print(f"MaxSAT solving completed in {solver_finished - solve_start:.4f} seconds")

    # print(assignment)

    # Output the schedule based on the assignment

    # if assignment:
    #     for m in range(1, nMeetings + 1):
    #         for t in range(1, nTotalSlots + 1):
    #             if x[m][t] in assignment:
    #                 print(f"Meeting {m} → Time slot {t}")

    # Build schedule serializations for the aggregate CSV.
    schedule_pairs = []
    meetings_per_slot = [[] for _ in range(nTotalSlots)]

    if assignment:
        positive_assignment = {lit for lit in assignment if lit > 0}
        for m in range(1, nMeetings + 1):
            for t in range(1, nTotalSlots + 1):
                if x[m][t] in positive_assignment:
                    schedule_pairs.append((m, t))
                    meetings_per_slot[t - 1].append(m)

    validation_status = 'NOT_RUN'

    if assignment:
        # Create a mapping of variable numbers to their assigned values
        var_assignment = {abs(var): (var > 0) for var in assignment}
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

        # y constraints: exact count and x <-> y channeling.
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

        # Exact prefix/suffix and internal gap-slot semantics for P*.
        for p in objective_encoding_participants:
            busy = [is_true(y[p][t]) for t in range(1, nTotalSlots + 1)]
            for t in range(1, nTotalSlots + 1):
                expected_prefix = any(busy[:t])
                expected_suffix = any(busy[t - 1:])
                assert is_true(prefix[p][t]) == expected_prefix, (
                    f"prefix[{p}][{t}] inconsistent"
                )
                assert is_true(suffix[p][t]) == expected_suffix, (
                    f"suffix[{p}][{t}] inconsistent"
                )

            expected_count = 0
            for t in range(2, nTotalSlots):
                expected_gap = any(busy[:t - 1]) and (not busy[t - 1]) and any(busy[t:])
                assert is_true(gap_slot[p][t]) == expected_gap, (
                    f"gap_slot[{p}][{t}] inconsistent"
                )
                expected_count += int(expected_gap)

            assert expected_count == participant_gap_slots[p - 1], (
                f"Business {p} gap total mismatch: "
                f"encoded={expected_count}, recomputed={participant_gap_slots[p - 1]}"
            )

            for j in range(1, max_gap_slots + 1):
                assert is_true(sortedGap[p][j]) == (expected_count >= j), (
                    f"sortedGap[{p}][{j}] inconsistent with gap total {expected_count}"
                )

        # Exact maximum/minimum unary values and P*-range objective.
        for j in range(1, max_gap_slots + 1):
            threshold_values = [
                is_true(sortedGap[p][j]) for p in objective_encoding_participants
            ]
            expected_max = any(threshold_values)
            expected_min = all(threshold_values) if threshold_values else True
            expected_dif = expected_max and (not expected_min)
            assert is_true(maxGap[j]) == expected_max, f"maxGap[{j}] inconsistent"
            assert is_true(minGap[j]) == expected_min, f"minGap[{j}] inconsistent"
            assert is_true(difGap[j]) == expected_dif, f"difGap[{j}] inconsistent"

        encoded_range = sum(
            is_true(difGap[j]) for j in range(1, max_gap_slots + 1)
        )
        assert encoded_range == idle_range_pstar, (
            f"Encoded objective={encoded_range}, "
            f"IdleRange(P*)={idle_range_pstar}"
        )
        encoded_maximum = sum(
            is_true(maxGap[j]) for j in range(1, max_gap_slots + 1)
        )
        assert encoded_maximum == maximum_internal_idle_slots
        if ARGS.participant_idle_cap_rule == 'interpolated_bounds':
            for p in objective_participants:
                assert participant_gap_slots[p - 1] <= participant_idle_caps[p]
        # Participant load bound per slot
        for t in range(1, nTotalSlots + 1):
            participants_at_t = sum(is_true(y[p][t]) for p in range(1, nBusiness + 1))
            assert participants_at_t <= 2 * nTables, (
                f"Too many participants at slot {t} (count={participants_at_t}, bound={2*nTables})"
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
        validation_status = 'PASSED'

    end_time = time.time()
    total_time = end_time - start_time
    print(f"\n{'='*60}")
    print(f"TOTAL RUNTIME: {total_time:.4f} seconds ({total_time:.2f}s)")
    print(f"{'='*60}")

    peak_memory_mb = memory_sampler.stop_mb()
    print(
        'Peak process-tree RSS: '
        f"{peak_memory_mb:.3f} MB" if peak_memory_mb is not None
        else 'Peak process-tree RSS: N/A (install psutil to enable it)'
    )

    clause_lengths = [len(clause) for clause in cnf.clauses]
    n_primary_variables = nMeetings * nTotalSlots
    all_participant_idle_range = (
        max(participant_gap_slots) - min(participant_gap_slots)
        if participant_gap_slots
        else None
    )
    baseline_result = {
        **instance_result_metadata(instance_spec),
        **experiment,
        'configuration_label': ORG_CONFIGURATION_LABEL,
        'configuration_id': ORG_CONFIGURATION_ID,
        'configuration_key': ORG_CONFIGURATION_ID,
        'factor_m': 'ORGFull',
        'factor_f': 'N/A',
        'factor_p': 'Pairwise',
        'factor_g': 'Direct-E',
        'factor_b': 'PerSlotCardinality',
        'factor_o': ORG_OBJECTIVE_FACTOR,
        'factor_s': 'UWrMaxSAT',
        'factor_i': ORG_IMPLIED_PACKAGE_NAME,
        'formalism': 'MaxSAT',
        'model_family': 'ORGHistorical',
        'formulation_name': 'ORG-Published-style-Idle',
        'model_family_display_name': 'Published-style',
        'model_configuration_display_name': 'Published-style/Full',
        'implementation_provenance': 'independent_reimplementation',
        'optimization_procedure_display_name': 'one-shot weighted MaxSAT',
        'domain_mode': 'legacy_full',
        'domain_filter_graph': 'n/a',
        'precedence_encoding': 'pairwise',
        'precedence_graph': 'direct',
        'optimization_engine': 'UWrMaxSAT',
        'solver_backend': solver_used,
        'solver_version': f'binary-sha256:{UWRMAXSAT_BINARY_SHA256}',
        'encoding_variant': ORG_ENCODING_VARIANT,
        'idle_encoding': 'per_slot_cardinality',
        'objective': ORG_OBJECTIVE_NAME,
        'objective_mode': ARGS.objective_mode,
        'objective_code': ORG_OBJECTIVE_CODE,
        'implied_constraints_code': ORG_IMPLIED_PACKAGE_CODE,
        'sat_result': status_to_sat_result(solve_status),
        'status': solve_status,
        'runtime_seconds': round(total_time, 6),
        'input_parsing_seconds': round(input_time - start_time, 6),
        'model_construction_seconds': round(constraint_time - input_time, 6),
        'model_build_seconds': round(constraint_time - start_time, 6),
        'solve_and_validate_seconds': round(end_time - constraint_time, 6),
        'solver_runtime_seconds': round(solver_finished - solve_start, 6),
        'runtime_scope': RUNTIME_SCOPE,
        'runtime_censored': solve_status == 'TIMEOUT',
        'peak_memory_mb': peak_memory_mb,
        'memory_metric': MEMORY_METRIC,
        'n_vars': variable_size,
        'n_primary_variables': n_primary_variables,
        'n_auxiliary_variables': variable_size - n_primary_variables,
        'n_hard_clauses': len(cnf.clauses),
        'n_soft_clauses': len(soft_clause_weights),
        'n_total_clauses': len(cnf.clauses) + len(soft_clause_weights),
        'n_hard_literals': sum(clause_lengths),
        'n_soft_literals': len(soft_clause_weights),
        'n_total_literals': sum(clause_lengths) + len(soft_clause_weights),
        'max_hard_clause_length': max(clause_lengths, default=0),
        'max_soft_clause_length': 1 if soft_clause_weights else 0,
        'n_unit_hard_clauses': sum(length == 1 for length in clause_lengths),
        'n_binary_hard_clauses': sum(length == 2 for length in clause_lengths),
        'n_ternary_hard_clauses': sum(length == 3 for length in clause_lengths),
        'n_long_hard_clauses': sum(length >= 4 for length in clause_lengths),
        'soft_clause_weight': (
            1 if ARGS.objective_mode == 'ir' and soft_clause_weights else None
        ),
        'soft_weight_sum': sum(soft_clause_weights),
        'max_soft_weight_bits': max(
            (weight.bit_length() for weight in soft_clause_weights),
            default=0,
        ),
        'n_objective_lits': len(soft_clause_weights),
        'n_optimizer_calls': 1,
        'n_bound_encodings': 0,
        'optimizer_added_variables_peak': 0,
        'optimizer_added_clauses_peak': 0,
        'optimizer_added_literals_peak': 0,
        'optimizer_added_clauses_cumulative': 0,
        'formula_scope': FORMULA_SCOPE,
        'full_schedule_candidates': n_primary_variables,
        'unary_eligible_schedule_candidates': None,
        'reduced_schedule_candidates': None,
        'active_schedule_candidates': n_primary_variables,
        'precedence_direct_edges': sum(
            len(predecessors) for predecessors in precedences[1:]
        ),
        'precedence_closure_edges': None,
        'precedence_relation_edges': sum(
            len(predecessors) for predecessors in precedences[1:]
        ),
        'precedence_max_distance': 1,
        'precedence_pairwise_clauses': None,
        'precedence_sparse_link_clauses': 0,
        'precedence_unique_suffix_cuts': 0,
        'precedence_mode': 'traditional',
        'precedence_configuration': 'pairwise+direct',
        'solver': solver_used,
        'solver_binary': str(UWRMAXSAT_BINARY),
        'solver_binary_sha256': UWRMAXSAT_BINARY_SHA256,
        'solver_command': shlex.join(solver_command),
        'solver_message': '',
        'solver_cost': solver_cost,
        'objective_value': (
            objective_vector[0]
            if objective_vector is not None
            else (solver_cost if ARGS.objective_mode == 'ir' else None)
        ),
        'best_value': (
            objective_vector[0]
            if objective_vector is not None
            else (solver_cost if ARGS.objective_mode == 'ir' else None)
        ),
        'proven_optimum': (
            objective_vector[0]
            if solve_status == 'OPTIMAL' and objective_vector is not None
            else None
        ),
        'objective_vector': (
            serialize_list(objective_vector) if objective_vector is not None else ''
        ),
        'proven_objective_vector': (
            serialize_list(objective_vector) if solve_status == 'OPTIMAL' else ''
        ),
        'primary_objective_value': (
            objective_vector[0] if objective_vector is not None else None
        ),
        'secondary_objective_value': (
            objective_vector[1]
            if objective_vector is not None and len(objective_vector) > 1
            else None
        ),
        'tertiary_objective_value': (
            objective_vector[2]
            if objective_vector is not None and len(objective_vector) > 2
            else None
        ),
        'lexicographic_scalar_cost': lexicographic_scalar_cost,
        'objective_tier_weights': serialize_list(objective_tier_weights),
        'idle_range_pstar': idle_range_pstar,
        'all_participant_idle_range': all_participant_idle_range,
        'total_internal_idle_slots': total_internal_idle_slots,
        'maximum_internal_idle_slots': maximum_internal_idle_slots,
        'squared_internal_idle_slots': squared_internal_idle_slots,
        'participant_internal_idle_slots': serialize_list(participant_gap_slots),
        'participant_idle_cap_rule': ARGS.participant_idle_cap_rule,
        'participant_idle_cap_alpha': ARGS.participant_idle_cap_alpha,
        'participant_idle_lower_bounds': serialize_list(idle_lower_zero),
        'participant_idle_upper_bounds': serialize_list(idle_upper_zero),
        'participant_idle_caps': serialize_list(idle_caps_zero),
        'participant_idle_bounds_feasible': idle_bounds_feasible,
        'participant_idle_cap_clause_count': participant_idle_cap_clause_count,
        'objective_participant_count': len(objective_participants),
        'objective_participants': serialize_list(objective_participants),
        'participant_gap_slots': serialize_list(participant_gap_slots),
        'assignment_by_meeting': serialize_assignment(schedule_pairs),
        'schedule_by_slot': serialize_schedule(meetings_per_slot),
        'validation_status': validation_status,
        'validation_errors': '',
        'error_type': '',
        'error_message': '',
    }
    csv_results.append(baseline_result)
    workbook_path = write_instance_workbook(
        EXCEL_OUTPUT_DIR / safe_workbook_name(instance_spec.instance_name),
        instance_spec.instance_name,
        [baseline_result],
    )
    print(f'Excel exported to: {workbook_path}')

    print(
        f"Result queued for CSV: {solve_status} | "
        f"objective={objective_vector if objective_vector is not None else 'N/A'} | "
        f"participant_idle_cap={ARGS.participant_idle_cap_rule}"
    )


# Xuất một bảng CSV tổng hợp sau khi chạy toàn bộ input.
write_detailed_csv(Path(CSV_OUTPUT_FILE), csv_results)

print(f"CSV exported to: {CSV_OUTPUT_FILE}")
