#!/usr/bin/env bash
# One Main-Objectives configuration per session, stress then stress_high.
# Usage: bash ~/run_main_stress_session.sh M01 0
set -Eeuo pipefail

usage() {
  echo 'Usage: bash run_main_stress_session.sh M01|M02|M03|M04|M05|M06 CPU_CORE' >&2
  exit 2
}
[[ $# -eq 2 ]] || usage
CONFIG_CODE=${1^^}
CPU_CORE=$2
[[ "$CONFIG_CODE" =~ ^M0[1-6]$ && "$CPU_CORE" =~ ^[0-9]+$ ]] || usage

PROJECT_DIR=${PROJECT_DIR:-"$HOME/Business-Meeting-Scheduling-Problem"}
cd "$PROJECT_DIR"
[[ "$(git branch --show-current)" == journal-compact-objectives ]] || {
  echo 'ERROR: expected branch journal-compact-objectives' >&2; exit 2;
}

ENV_FILE=${JOURNAL_ENV_FILE:-"$PROJECT_DIR/journal_gcp.env"}
[[ -f "$ENV_FILE" ]] || { echo "ERROR: missing $ENV_FILE" >&2; exit 2; }

# Preserve explicit overrides supplied when launching this session.
RETRY_ERRORS_OVERRIDE=${RETRY_ERRORS-}
DRIFT_OVERRIDE=${ALLOW_ENVIRONMENT_DRIFT-}

# shellcheck disable=SC1090
source "$ENV_FILE"

if [[ -n "$RETRY_ERRORS_OVERRIDE" ]]; then
  RETRY_ERRORS="$RETRY_ERRORS_OVERRIDE"
fi

if [[ -n "$DRIFT_OVERRIDE" ]]; then
  ALLOW_ENVIRONMENT_DRIFT="$DRIFT_OVERRIDE"
fi

PYTHON="$PROJECT_DIR/${JOURNAL_VENV:-.venv-ubuntu}/bin/python"
[[ -x "$PYTHON" ]] || { echo "ERROR: missing $PYTHON" >&2; exit 2; }
[[ -x "${UWRMAXSAT_BIN:-}" ]] || { echo 'ERROR: missing UWRMAXSAT_BIN' >&2; exit 2; }
[[ "${UWRMAXSAT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || {
  echo 'ERROR: invalid UWRMAXSAT_SHA256' >&2; exit 2;
}
ACTUAL_HASH=$(sha256sum "$UWRMAXSAT_BIN" | awk '{print $1}')
[[ "$ACTUAL_HASH" == "$UWRMAXSAT_SHA256" ]] || {
  echo "ERROR: solver hash mismatch: $ACTUAL_HASH" >&2; exit 2;
}
taskset -c "$CPU_CORE" true 2>/dev/null || {
  echo "ERROR: CPU_CORE=$CPU_CORE is not available; check taskset -pc \$\$" >&2; exit 2;
}

OUTPUT_ROOT=${OUTPUT_ROOT:-"$PROJECT_DIR/outputs/journal"}
mkdir -p "$OUTPUT_ROOT"
OUTPUT_ROOT=$(realpath "$OUTPUT_ROOT")
CONFIG_DIR="$OUTPUT_ROOT/main-stress-configs"
OUTPUT_DIR="$OUTPUT_ROOT/main-stress-sessions/${CONFIG_CODE,,}"
CONFIG_FILE="$CONFIG_DIR/${CONFIG_CODE,,}.json"
mkdir -p "$CONFIG_DIR" "$OUTPUT_DIR"

# Session-local lock; unlike scripts/run_journal_gcp.sh, does not lock all campaigns.
exec 9>"$OUTPUT_DIR/.session.lock"
if ! flock -n 9; then
  echo "ERROR: $CONFIG_CODE is already running against $OUTPUT_DIR" >&2
  exit 2
fi

# Build a config by COPYING one exact configuration from the checked-in blocks file.
# No reuse: each of the two datasets is executed once, independently.
"$PYTHON" - "$CONFIG_CODE" "$CONFIG_FILE" <<'PY'
import json
import pathlib
import sys

code, destination = sys.argv[1:]
root = pathlib.Path.cwd()
base_file = root / 'journal_configs' / 'main_objectives.json'
with base_file.open(encoding='utf-8') as f:
    base = json.load(f)
blocks_file = (base_file.parent / base.get('blocks_from', 'main_objectives_blocks.json')).resolve()
if not blocks_file.is_file():
    raise SystemExit(f'ERROR: missing checked-in blocks file: {blocks_file}')
with blocks_file.open(encoding='utf-8') as f:
    block_source = json.load(f)

# Find the main 2-model x 3-objective block by the exact reuse rule in main_objectives.json.
reuse = base.get('reuse_results', [])
main_block_ids = {rule['target_block_id'] for rule in reuse}
if len(main_block_ids) != 1:
    raise SystemExit(f'ERROR: cannot identify main comparison block: {main_block_ids}')
main_block_id = next(iter(main_block_ids))
matching = [b for b in block_source.get('blocks', []) if b.get('id') == main_block_id]
if len(matching) != 1:
    raise SystemExit(f'ERROR: expected exactly one {main_block_id!r} block')
original_block = matching[0]
configs = original_block.get('configurations', [])
if len(configs) != 6:
    raise SystemExit(f'ERROR: expected six original main configurations, found {len(configs)}')

# Compact IDs are NOT guessed; they come from the published source-to-target reuse rules.
compact_ids = {
    rule['target_configuration_id']: rule['source_configuration_id']
    for rule in reuse
}
expected_compact_ids = {
    'compact_acf_bg_d2': 'M02',
    'compact_cdf_ir': 'M04',
    'compact_cdf_ir_im_is': 'M06',
}
if set(compact_ids) != set(expected_compact_ids):
    raise SystemExit(f'ERROR: unexpected Compact main IDs: {sorted(compact_ids)}')

by_code = {}
for conf in configs:
    cid = conf['id']
    if cid in expected_compact_ids:
        key = expected_compact_ids[cid]
        if conf.get('executor') != 'main':
            raise SystemExit(f'ERROR: {cid} is not the expected Compact executor')
    else:
        executor = conf.get('executor')
        objective = conf.get('objective_mode', {
            'org_bg_d2': 'bg_d2', 'org_ir': 'ir'
        }.get(executor, ''))
        baseline_map = {
            ('bg_d2', 'org_bg_d2'): 'M01',
            ('ir', 'org_ir'): 'M03',
            ('ir_im_is', 'org_idle'): 'M05',
        }
        key = baseline_map.get((objective, executor))
        if key is None:
            raise SystemExit(
                f'ERROR: unrecognized 2022 baseline {cid!r} '
                f'(executor={executor!r}, objective_mode={objective!r})'
            )
    if key in by_code:
        raise SystemExit(f'ERROR: duplicate mapping for {key}')
    by_code[key] = conf
if set(by_code) != {'M01', 'M02', 'M03', 'M04', 'M05', 'M06'}:
    raise SystemExit(f'ERROR: incomplete main config mapping: {sorted(by_code)}')

selected = dict(by_code[code])
result = {key: value for key, value in base.items()
          if key not in ('blocks_from', 'reuse_results', 'expected_job_count',
                         'block_repetitions', 'block_datasets', 'resolved_blocks_from',
                         'resolved_blocks_sha256')}
result['campaign_name'] = f"journal-main-stress-{code.lower()}-v1"
result['description'] = (
    f"Original main configuration {selected['id']} on stress and stress_high, "
    "1 repetition per instance, no reused results"
)
result['datasets'] = [
    {'id': 'stress', 'manifest': 'data_precedence_stress/instances_manifest.csv',
     'family': 'all'},
    {'id': 'stress_high', 'manifest': 'data_precedence_stress_high/instances_manifest.csv',
     'family': 'all'},
]
result['blocks'] = [
    {'id': f'{main_block_id}_stress', 'datasets': ['stress'],
     'repetitions': 1, 'configurations': [selected]},
    {'id': f'{main_block_id}_stress_high', 'datasets': ['stress_high'],
     'repetitions': 1, 'configurations': [selected]},
]
# Expected job count depends on the actual manifests; Journal_Experiment validates
# every planned job from those manifests, including content-ID de-duplication.
new_text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
output = pathlib.Path(destination)
if output.is_file() and output.read_text(encoding='utf-8') != new_text:
    raise SystemExit(f'ERROR: existing config changed: {output}. Do not overwrite an active/resumable campaign.')
if not output.exists():
    output.write_text(new_text, encoding='utf-8')
print(f'Selected: {code} -> {selected["id"]}, executor={selected["executor"]}')
print('Datasets: stress, stress_high | 1 repetition each | no reuse')
print(f'Config: {output}')
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

# Plan first so the job count and input selections are visible before any solving.
"$PYTHON" -u src/Journal_Experiment.py \
  --config "$CONFIG_FILE" --output-dir "$OUTPUT_DIR" --plan-only
"$PYTHON" - "$OUTPUT_DIR/plan.json" <<'PY'
import collections, json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    p = json.load(f)
counts = collections.Counter(j['experiment_block'].rsplit('_', 1)[-1]
                             for j in p['jobs'])
# Do not infer the dataset from content family, because both manifests may
# use the same family label. The block ID explicitly records the dataset.
blocks = collections.Counter(j['experiment_block'] for j in p['jobs'])
assert len(blocks) == 2, f'Expected both datasets, got: {blocks}'
assert all(j['repetition'] == 1 for j in p['jobs'])
assert len({j['planned_configuration_id'] for j in p['jobs']}) == 1
for name, n in blocks.items():
    print(f'PLANNED {name}: {n} jobs')
print(f'TOTAL: {p["job_count"]} jobs')
PY

echo "RUNNING $CONFIG_CODE on CPU_CORE=$CPU_CORE; output=$OUTPUT_DIR"
# Per-cell detailed logs are written by Journal_Experiment.py in OUTPUT_DIR/logs.
# --resume is used because --plan-only has created plan.json above.
taskset -c "$CPU_CORE" "$PYTHON" -u src/Journal_Experiment.py \
  "${COMMON_ARGS[@]}" --resume 2>&1 | tee -a "$OUTPUT_DIR/session.log"
