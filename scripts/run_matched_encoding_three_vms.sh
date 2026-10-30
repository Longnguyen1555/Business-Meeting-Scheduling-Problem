#!/usr/bin/env bash
# Bootstrap the frozen campaign, then run one worker on each identical GCP VM.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

dirty_override=${ALLOW_DIRTY-}
retry_override=${RETRY_ERRORS-}
env_file=${JOURNAL_ENV_FILE:-$PROJECT_DIR/journal_gcp.env}
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
# Exported files differ from the base tree; exact source hashes are still required.
export ALLOW_DIRTY=${dirty_override:-1}
if [[ -n "$retry_override" ]]; then export RETRY_ERRORS=$retry_override; fi

# Resolve the original checkout's environment before entering the frozen runtime.
if [[ -z "${JOURNAL_PYTHON:-}" ]]; then
  venv_dir=${JOURNAL_VENV:-$PROJECT_DIR/.venv-ubuntu}
  if [[ "$venv_dir" != /* ]]; then venv_dir=$PROJECT_DIR/$venv_dir; fi
  if [[ -x "$venv_dir/bin/python" ]]; then export JOURNAL_PYTHON=$venv_dir/bin/python; fi
fi
# The configuration has already been sourced and exported above.
export JOURNAL_ENV_FILE=/dev/null
exec python3 -B "$PROJECT_DIR/src/Bootstrap_Matched_Evaluation.py" "${@:-plan}"
