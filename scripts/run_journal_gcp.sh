#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

ENV_FILE=${JOURNAL_ENV_FILE:-$PROJECT_DIR/journal_gcp.env}
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: copy journal_gcp.env.example to $ENV_FILE and fill the pins" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

JOURNAL_VENV=${JOURNAL_VENV:-.venv-ubuntu}
OUTPUT_ROOT=${OUTPUT_ROOT:-$PROJECT_DIR/outputs/journal}
ALLOW_DIRTY=${ALLOW_DIRTY:-0}
RETRY_ERRORS=${RETRY_ERRORS:-0}

if [[ ! -x "$JOURNAL_VENV/bin/python" ]]; then
  echo "ERROR: missing Python environment $JOURNAL_VENV" >&2
  exit 2
fi
if [[ -z "${UWRMAXSAT_BIN:-}" || ! -x "$UWRMAXSAT_BIN" ]]; then
  echo "ERROR: UWRMAXSAT_BIN is missing or not executable" >&2
  exit 2
fi
if [[ ! "${UWRMAXSAT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "ERROR: UWRMAXSAT_SHA256 is not a 64-digit lowercase hash" >&2
  exit 2
fi
actual_hash=$(sha256sum "$UWRMAXSAT_BIN" | awk '{print $1}')
if [[ "$actual_hash" != "$UWRMAXSAT_SHA256" ]]; then
  echo "ERROR: UWrMaxSAT hash mismatch: $actual_hash" >&2
  exit 2
fi
if [[ "$ALLOW_DIRTY" != "1" && -n "$(git status --porcelain)" ]]; then
  echo "ERROR: production runs require a clean worktree" >&2
  git status --short >&2
  exit 2
fi

mkdir -p "$OUTPUT_ROOT"
exec 9>"$OUTPUT_ROOT/.campaign.lock"
if ! flock -n 9; then
  echo "ERROR: another journal campaign holds $OUTPUT_ROOT/.campaign.lock" >&2
  exit 2
fi

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTHONHASHSEED=0

PYTHON="$JOURNAL_VENV/bin/python"
RUNNER=("$PYTHON" -u src/Journal_Experiment.py)
if [[ -n "${CPU_CORE:-}" ]]; then
  if ! taskset -c "$CPU_CORE" true >/dev/null 2>&1; then
    echo "ERROR: CPU_CORE=$CPU_CORE is not available to this VM/process" >&2
    exit 2
  fi
  RUNNER=(taskset -c "$CPU_CORE" "${RUNNER[@]}")
fi

runner_common=(
  --uwrmaxsat-bin "$UWRMAXSAT_BIN"
  --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256"
)
if [[ "$ALLOW_DIRTY" == "1" ]]; then
  runner_common+=(--allow-dirty)
fi
if [[ "$RETRY_ERRORS" == "1" ]]; then
  runner_common+=(--retry-errors)
fi

run_campaign() {
  local config_name=$1
  local output_name=$2
  shift 2
  local output_dir="$OUTPUT_ROOT/$output_name"
  local resume_args=()
  if [[ -f "$output_dir/plan.json" ]]; then
    resume_args+=(--resume)
  fi
  local runner_status=0
  local validator_status=0
  "${RUNNER[@]}" \
    --config "journal_configs/$config_name.json" \
    --output-dir "$output_dir" \
    "${runner_common[@]}" \
    "${resume_args[@]}" \
    "$@" || runner_status=$?
  validate_existing "$output_dir" || validator_status=$?
  if [[ "$runner_status" -ne 0 ]]; then
    return "$runner_status"
  fi
  return "$validator_status"
}

plan_campaign() {
  local config_name=$1
  local output_name=$2
  shift 2
  local output_dir="$OUTPUT_ROOT/$output_name"
  local resume_args=()
  if [[ -f "$output_dir/plan.json" ]]; then
    resume_args+=(--resume)
  fi
  "${RUNNER[@]}" \
    --config "journal_configs/$config_name.json" \
    --output-dir "$output_dir" \
    --plan-only \
    "${resume_args[@]}" \
    "$@"
}

generate_data() {
  if [[ -d data_journal_generated ]]; then
    "$PYTHON" src/Generate_Journal_Benchmark.py validate \
      --data-dir data_journal_generated
  else
    "$PYTHON" src/Generate_Journal_Benchmark.py generate \
      --output-dir data_journal_generated \
      --n-development 240 \
      --n-heldout 60 \
      --master-seed 20260808
    "$PYTHON" src/Generate_Journal_Benchmark.py validate \
      --data-dir data_journal_generated
  fi
}

require_generated_data() {
  if [[ ! -d data_journal_generated ]]; then
    echo "ERROR: run '$0 generate', audit the manifests, and commit the frozen dataset first" >&2
    exit 2
  fi
  "$PYTHON" src/Generate_Journal_Benchmark.py validate \
    --data-dir data_journal_generated
}

extract_features() {
  local feature_file="$OUTPUT_ROOT/instance_features.csv"
  if [[ -f "$feature_file" ]]; then
    echo "feature file already frozen: $feature_file"
    return
  fi
  "$PYTHON" src/Journal_Instance_Features.py \
    --dataset official:instances_manifest.csv \
    --dataset stress:data_precedence_stress/instances_manifest.csv \
    --dataset stress_high:data_precedence_stress_high/instances_manifest.csv \
    --dataset generated:data_journal_generated/instances_manifest.csv \
    --family all \
    --output "$feature_file"
}

audit_coverage() {
  local audit_dir="$OUTPUT_ROOT/coverage_audit"
  if [[ -f "$audit_dir/coverage_summary.json" ]]; then
    echo "coverage audit already frozen: $audit_dir/coverage_summary.json"
    return
  fi
  "$PYTHON" src/Audit_Journal_Coverage.py \
    --features "$OUTPUT_ROOT/instance_features.csv" \
    --generation-manifest data_journal_generated/generation_manifest.csv \
    --output-dir "$audit_dir"
}

validate_existing() {
  local output_dir=$1
  local dirty_args=()
  if [[ "$ALLOW_DIRTY" == "1" ]]; then
    dirty_args+=(--allow-dirty)
  fi
  "$PYTHON" src/Validate_Journal_Run.py \
    --output "$output_dir" "${dirty_args[@]}"
}

run_warmup() {
  local boot_id
  local commit_id
  boot_id=$(cat /proc/sys/kernel/random/boot_id)
  commit_id=$(git rev-parse --short=12 HEAD)
  run_campaign warmup "warmup/$boot_id/$commit_id"
}

command=${1:-help}
case "$command" in
  setup)
    "$SCRIPT_DIR/setup_journal_gcp.sh"
    ;;
  generate)
    generate_data
    ;;
  features)
    require_generated_data
    extract_features
    audit_coverage
    ;;
  coverage)
    require_generated_data
    extract_features
    audit_coverage
    ;;
  correctness)
    run_campaign correctness correctness-development
    ;;
  smoke)
    run_campaign production_smoke production-smoke
    ;;
  pilot)
    require_generated_data
    run_warmup
    run_campaign pilot stratified-pilot
    ;;
  official)
    run_warmup
    run_campaign official_core official-core
    ;;
  precedence)
    run_warmup
    run_campaign precedence_ablation precedence-ablation
    ;;
  generated-development)
    require_generated_data
    run_warmup
    run_campaign generated_core generated-development \
      --only-block e5_generated_development
    ;;
  generated-heldout)
    if [[ "${HELDOUT_FROZEN:-}" != "YES" ]]; then
      echo "ERROR: set HELDOUT_FROZEN=YES only after confirming the fixed timeout and freezing model/analysis" >&2
      exit 2
    fi
    require_generated_data
    run_warmup
    run_campaign generated_core generated-heldout \
      --only-block e5_generated_heldout
    ;;
  plan)
    require_generated_data
    plan_campaign production_smoke production-smoke
    plan_campaign pilot stratified-pilot
    plan_campaign official_core official-core
    plan_campaign precedence_ablation precedence-ablation
    plan_campaign generated_core generated-development \
      --only-block e5_generated_development
    plan_campaign generated_core generated-heldout \
      --only-block e5_generated_heldout
    ;;
  all-development)
    require_generated_data
    extract_features
    audit_coverage
    run_campaign correctness correctness-development
    run_campaign production_smoke production-smoke
    run_warmup
    run_campaign pilot stratified-pilot
    echo "Pilot complete. Confirm compute/matrix/analysis; cutoff remains fixed at 7200 seconds."
    ;;
  validate)
    if [[ $# -ne 2 ]]; then
      echo "usage: $0 validate OUTPUT_DIRECTORY" >&2
      exit 2
    fi
    validate_existing "$2"
    ;;
  help|-h|--help)
    cat <<'EOF'
Usage: scripts/run_journal_gcp.sh COMMAND

Commands:
  setup                  verify the existing conference environment
  generate               freeze/validate Generated-300
  features               extract all pre-solve feature rows once
  coverage               extract features and audit marginal/joint coverage
  correctness             local RC2/CaDiCaL 12-content gate
  smoke                   pinned production solver 12-content gate
  pilot                   7,200-second development-only stratified pilot
  official                E1-E3 All-126 (8,820 runs)
  precedence              E4 2x2x2 ablation (3,360 runs)
  generated-development   E5 Development-240 only
  generated-heldout       E5 Held-out-60; requires HELDOUT_FROZEN=YES
  plan                    create and report all deterministic plans
  all-development         features, coverage, correctness, smoke and pilot
  validate DIR            strictly validate one completed campaign

Every campaign is single-worker, append-only and resumable. Re-run the same
command after interruption; the script adds --resume automatically.
Set RETRY_ERRORS=1 only to create a new attempt for existing ERROR rows.
EOF
    ;;
  *)
    echo "ERROR: unknown command: $command" >&2
    exit 2
    ;;
esac
