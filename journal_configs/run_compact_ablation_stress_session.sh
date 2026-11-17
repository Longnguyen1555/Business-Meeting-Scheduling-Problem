#!/usr/bin/env bash
# One original Compact-Ablation configuration per tmux session.
# Each session runs exactly 1 repetition on stress (60) and stress_high (40).
# Usage: bash journal_configs/run_compact_ablation_stress_session.sh A01 0
#        bash journal_configs/run_compact_ablation_stress_session.sh A01 0 --plan-only
#        bash journal_configs/run_compact_ablation_stress_session.sh A01 0 --max-runs 1
set -Eeuo pipefail

usage() {
  echo 'Usage: bash run_compact_ablation_stress_session.sh A01..A15 CPU_CORE [--plan-only | --max-runs N]' >&2
  exit 2
}
[[ $# -ge 2 && $# -le 4 ]] || usage
CONFIG_CODE=${1^^}
CPU_CORE=$2
[[ "$CONFIG_CODE" =~ ^A(0[1-9]|1[0-5])$ && "$CPU_CORE" =~ ^[0-9]+$ ]] || usage

PLAN_ONLY=0
MAX_RUNS=''
case "${3-}" in
  '') [[ $# -eq 2 ]] || usage ;;
  --plan-only) [[ $# -eq 3 ]] || usage; PLAN_ONLY=1 ;;
  --max-runs)
    [[ $# -eq 4 && "${4-}" =~ ^[1-9][0-9]*$ ]] || usage
    MAX_RUNS=$4
    ;;
  *) usage ;;
esac

PROJECT_DIR=${PROJECT_DIR:-"$HOME/Business-Meeting-Scheduling-Problem"}
cd "$PROJECT_DIR"
[[ "$(git branch --show-current)" == journal-compact-objectives ]] || {
  echo 'ERROR: expected branch journal-compact-objectives' >&2
  exit 2
}

ENV_FILE=${JOURNAL_ENV_FILE:-"$PROJECT_DIR/journal_gcp.env"}
[[ -f "$ENV_FILE" ]] || { echo "ERROR: missing $ENV_FILE" >&2; exit 2; }
# Prevent the sourced journal_gcp.env from overwriting deliberate CLI overrides.
RETRY_ERRORS_OVERRIDE=${RETRY_ERRORS-}
DRIFT_OVERRIDE=${ALLOW_ENVIRONMENT_DRIFT-}
DIRTY_OVERRIDE=${ALLOW_DIRTY-}
# shellcheck disable=SC1090
source "$ENV_FILE"
if [[ -n "$RETRY_ERRORS_OVERRIDE" ]]; then RETRY_ERRORS=$RETRY_ERRORS_OVERRIDE; fi
if [[ -n "$DRIFT_OVERRIDE" ]]; then ALLOW_ENVIRONMENT_DRIFT=$DRIFT_OVERRIDE; fi
if [[ -n "$DIRTY_OVERRIDE" ]]; then ALLOW_DIRTY=$DIRTY_OVERRIDE; fi

VENV_DIR=${JOURNAL_VENV:-.venv-ubuntu}
if [[ "$VENV_DIR" != /* ]]; then VENV_DIR="$PROJECT_DIR/$VENV_DIR"; fi
PYTHON="$VENV_DIR/bin/python"
[[ -x "$PYTHON" ]] || { echo "ERROR: missing Python: $PYTHON" >&2; exit 2; }
[[ -x "${UWRMAXSAT_BIN:-}" ]] || { echo 'ERROR: missing UWRMAXSAT_BIN' >&2; exit 2; }
[[ "${UWRMAXSAT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || {
  echo 'ERROR: invalid UWRMAXSAT_SHA256' >&2; exit 2;
}
ACTUAL_HASH=$(sha256sum "$UWRMAXSAT_BIN" | awk '{print $1}')
[[ "$ACTUAL_HASH" == "$UWRMAXSAT_SHA256" ]] || {
  echo "ERROR: UWrMaxSAT hash mismatch: $ACTUAL_HASH" >&2; exit 2;
}
taskset -c "$CPU_CORE" true 2>/dev/null || {
  echo "ERROR: CPU_CORE=$CPU_CORE unavailable; check taskset -pc \$\$" >&2; exit 2;
}

OUTPUT_ROOT=${OUTPUT_ROOT:-"$PROJECT_DIR/outputs/journal"}
mkdir -p "$OUTPUT_ROOT"
OUTPUT_ROOT=$(realpath "$OUTPUT_ROOT")
CONFIG_DIR="$OUTPUT_ROOT/compact-ablation-stress-configs"
OUTPUT_DIR="$OUTPUT_ROOT/compact-ablation-stress-sessions/${CONFIG_CODE,,}"
CONFIG_FILE="$CONFIG_DIR/${CONFIG_CODE,,}.json"
mkdir -p "$CONFIG_DIR" "$OUTPUT_DIR"

# One exclusive lock per configuration. No global campaign lock; safe for two sessions/VM.
exec 9>"$OUTPUT_DIR/.session.lock"
if ! flock -n 9; then
  echo "ERROR: $CONFIG_CODE is already active against $OUTPUT_DIR" >&2
  exit 2
fi

# Derive the exact configuration directly from the Git-checked-in ablation blocks.
# All original solver/encoding options are copied without modifying them.
"$PYTHON" - "$CONFIG_CODE" "$CONFIG_FILE" <<'PY'
import json
import pathlib
import sys

code, destination = sys.argv[1:]
root = pathlib.Path.cwd()
base_file = root / 'journal_configs' / 'compact_objectives.json'
with base_file.open(encoding='utf-8') as stream:
    base = json.load(stream)
source_file = (base_file.parent / base.get('blocks_from', 'compact_objectives_blocks.json')).resolve()
with source_file.open(encoding='utf-8') as stream:
    source = json.load(stream)

# Frozen configuration order/IDs verified against compact_objectives_blocks.json.
expected = (
    ('compact_bg_d2_ablation', (
        'bg_reference', 'bg_a', 'bg_c', 'bg_ac', 'bg_acf')),
    ('compact_ir_ablation', (
        'ir_reference', 'ir_c', 'ir_d', 'ir_cd', 'ir_cdf')),
    ('compact_ir_im_is_ablation', (
        'irimis_reference', 'irimis_c', 'irimis_d', 'irimis_cd', 'irimis_cdf')),
)
blocks = source.get('blocks', [])
actual = tuple(
    (block['id'], tuple(conf['id'] for conf in block.get('configurations', [])))
    for block in blocks
)
if actual != expected:
    raise SystemExit(
        'ERROR: frozen compact ablation blocks/IDs differ from the validated 15-cell design. '
        f'Actual: {actual!r}'
    )
selected_by_code = {}
for index, (block, cid) in enumerate(
    ((block, conf['id']) for block in blocks for conf in block['configurations']),
    start=1,
):
    conf = next(c for c in block['configurations'] if c['id'] == cid)
    selected_by_code[f'A{index:02d}'] = (block['id'], dict(conf))

block_id, chosen = selected_by_code[code]
assert chosen['executor'] == 'main' and chosen['solver'] == 'maxsat'
assert chosen.get('maxsat_backend') == 'uwrmaxsat'

# The three full-factor ablation cells must match the completed compact main models,
# except for their intended different configuration IDs.
full_cell_to_main = {
    'bg_acf': 'compact_acf_bg_d2',
    'ir_cdf': 'compact_cdf_ir',
    'irimis_cdf': 'compact_cdf_ir_im_is',
}
if chosen['id'] in full_cell_to_main:
    main_path = root / 'journal_configs' / 'main_objectives_blocks.json'
    with main_path.open(encoding='utf-8') as stream:
        main_blocks = json.load(stream)['blocks']
    main_candidates = [
        c for b in main_blocks for c in b['configurations']
        if c['id'] == full_cell_to_main[chosen['id']]
    ]
    if len(main_candidates) != 1:
        raise SystemExit('ERROR: could not match full ablation to compact main configuration')
    without_id = lambda c: {k: v for k, v in c.items() if k != 'id'}
    if without_id(chosen) != without_id(main_candidates[0]):
        raise SystemExit('ERROR: full-factor ablation differs from its main configuration')

excluded = {
    'blocks_from', 'reuse_results', 'expected_job_count', 'block_repetitions',
    'block_datasets', 'resolved_blocks_from', 'resolved_blocks_sha256', 'blocks',
}
result = {k: v for k, v in base.items() if k not in excluded}
result['campaign_name'] = f'journal-compact-ablation-stress-{code.lower()}-v1'
result['description'] = (
    f'Compact ablation {chosen["id"]}; stress + stress_high; '
    'one repetition per instance; independent execution; no reuse'
)
result['expected_job_count'] = 100
result['datasets'] = [
    {'id': 'stress', 'manifest': 'data_precedence_stress/instances_manifest.csv',
     'family': 'all'},
    {'id': 'stress_high', 'manifest': 'data_precedence_stress_high/instances_manifest.csv',
     'family': 'all'},
]
result['blocks'] = [
    {'id': f'{block_id}_stress', 'datasets': ['stress'], 'repetitions': 1,
     'configurations': [chosen]},
    {'id': f'{block_id}_stress_high', 'datasets': ['stress_high'], 'repetitions': 1,
     'configurations': [chosen]},
]
text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
output = pathlib.Path(destination)
if output.is_file():
    if output.read_text(encoding='utf-8') != text:
        raise SystemExit(
            f'ERROR: existing generated config differs: {output}. '
            'Do not overwrite an active/resumable campaign.'
        )
else:
    output.write_text(text, encoding='utf-8')
print(f'SELECTED {code}: block={block_id}, id={chosen["id"]}, '
      f'encoding={chosen["compact_encoding"]}, AMO={chosen["collision_amo"]}', flush=True)
print('DATASETS stress=60, stress_high=40; repetitions=1; independent, no reuse', flush=True)
print(f'CONFIG {output}', flush=True)
PY

export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 PYTHONHASHSEED=0
COMMON_ARGS=(
  --config "$CONFIG_FILE"
  --output-dir "$OUTPUT_DIR"
  --uwrmaxsat-bin "$UWRMAXSAT_BIN"
  --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256"
)
if [[ "${ALLOW_DIRTY:-0}" == 1 ]]; then COMMON_ARGS+=(--allow-dirty); fi
if [[ "${ALLOW_ENVIRONMENT_DRIFT:-0}" == 1 ]]; then
  COMMON_ARGS+=(--allow-environment-drift)
fi
if [[ "${RETRY_ERRORS:-0}" == 1 ]]; then COMMON_ARGS+=(--retry-errors); fi
if [[ -n "$MAX_RUNS" ]]; then COMMON_ARGS+=(--max-runs "$MAX_RUNS"); fi

"$PYTHON" -u src/Journal_Experiment.py \
  --config "$CONFIG_FILE" --output-dir "$OUTPUT_DIR" --plan-only
"$PYTHON" - "$OUTPUT_DIR/plan.json" "$CONFIG_CODE" <<'PY'
import collections
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
code = sys.argv[2]
counts = collections.Counter(job['experiment_block'] for job in plan['jobs'])
by_dataset = {}
for block, count in counts.items():
    dataset = 'stress_high' if block.endswith('_stress_high') else (
        'stress' if block.endswith('_stress') else None
    )
    assert dataset is not None, f'Unexpected block name: {block}'
    assert dataset not in by_dataset, f'Duplicate dataset block: {dataset}'
    by_dataset[dataset] = count
assert by_dataset == {'stress': 60, 'stress_high': 40}, by_dataset
assert plan['job_count'] == 100
assert all(j['repetition'] == 1 for j in plan['jobs'])
assert len({j['planned_configuration_id'] for j in plan['jobs']}) == 1
assert plan['campaign_id'] == f'journal-compact-ablation-stress-{code.lower()}-v1'
print(f'PLAN VERIFIED: {code} => stress=60, stress_high=40, total=100', flush=True)
PY

if [[ "$PLAN_ONLY" == 1 ]]; then
  echo "PLAN-ONLY PASS: $CONFIG_CODE; no solver was launched"
  exit 0
fi

echo "RUNNING $CONFIG_CODE CPU_CORE=$CPU_CORE; output=$OUTPUT_DIR"
echo "RESUME FLAGS: retry_errors=${RETRY_ERRORS:-0}, allow_environment_drift=${ALLOW_ENVIRONMENT_DRIFT:-0}"
taskset -c "$CPU_CORE" "$PYTHON" -u src/Journal_Experiment.py \
  "${COMMON_ARGS[@]}" --resume 2>&1 | tee -a "$OUTPUT_DIR/session.log"
