#!/usr/bin/env bash
# One worker on each of three identical GCP VMs. Re-running resumes that shard.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

command_name=${1:-plan}
bundle_dir=${MATCHED_OUTPUT:-$PROJECT_DIR/outputs/journal/matched-encoding-v2}
plan_python=${PLAN_PYTHON:-${JOURNAL_PYTHON:-python3}}

case "$command_name" in
  prepare)
    exec "$plan_python" -B src/Matched_Encoding_Evaluation.py prepare --output "$bundle_dir"
    ;;
  plan)
    exec "$plan_python" -B src/Matched_Encoding_Evaluation.py plan --plan-only --output "$bundle_dir"
    ;;
  0|1|2)
    dirty_override=${ALLOW_DIRTY-}
    retry_override=${RETRY_ERRORS-}
    env_file=${JOURNAL_ENV_FILE:-$PROJECT_DIR/journal_gcp.env}
    if [[ -f "$env_file" ]]; then
      # shellcheck disable=SC1090
      source "$env_file"
    fi
    if [[ -n "$dirty_override" ]]; then ALLOW_DIRTY=$dirty_override; fi
    if [[ -n "$retry_override" ]]; then RETRY_ERRORS=$retry_override; fi
    run_python=${JOURNAL_PYTHON:-$PROJECT_DIR/${JOURNAL_VENV:-.venv-ubuntu}/bin/python}
    if [[ ! -x "$run_python" ]]; then
      echo "ERROR: set JOURNAL_PYTHON to the GCP Python environment" >&2
      exit 2
    fi
    if [[ -z "${UWRMAXSAT_BIN:-}" || -z "${UWRMAXSAT_SHA256:-}" ]]; then
      echo "ERROR: set UWRMAXSAT_BIN and UWRMAXSAT_SHA256 in journal_gcp.env" >&2
      exit 2
    fi
    extra_args=()
    export PYTHONHASHSEED=0
    # The exported source snapshot can include uncommitted implementation files;
    # all of their hashes and the base Git commit are verified before each run.
    if [[ "${ALLOW_DIRTY:-1}" == 1 ]]; then extra_args+=(--allow-dirty); fi
    if [[ "${RETRY_ERRORS:-0}" == 1 ]]; then extra_args+=(--retry-errors); fi
    if [[ -n "${CPU_CORE:-}" ]]; then extra_args+=(--cpu-core "$CPU_CORE"); fi
    exec "$run_python" -u -B src/Matched_Encoding_Evaluation.py run \
      --output "$bundle_dir" --shard-index "$command_name" \
      --uwrmaxsat-bin "$UWRMAXSAT_BIN" --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256" \
      "${extra_args[@]}"
    ;;
  merge)
    shift
    if [[ $# -ne 3 ]]; then
      echo "Usage: $0 merge SHARD_0_DIR SHARD_1_DIR SHARD_2_DIR" >&2
      exit 2
    fi
    exec "$plan_python" -B src/Merge_Journal_Shards.py \
      --input "$1" --input "$2" --input "$3" \
      --output "${MATCHED_MERGED_OUTPUT:-$bundle_dir/merged}" \
      --allow-dirty --allow-mixed-environments
    ;;
  analyze)
    exec "$plan_python" -B src/Cross_Generation_Analysis.py \
      --matched-results "${MATCHED_MERGED_OUTPUT:-$bundle_dir/merged}/normalized/detailed.csv"
    ;;
  *)
    echo "Usage: $0 prepare|plan|0|1|2|merge DIR0 DIR1 DIR2|analyze" >&2
    exit 2
    ;;
esac
