#!/usr/bin/env bash
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
export ALLOW_DIRTY=${dirty_override:-1}
if [[ -n "$retry_override" ]]; then export RETRY_ERRORS=$retry_override; fi

if [[ -z "${JOURNAL_PYTHON:-}" ]]; then
  venv_dir=${JOURNAL_VENV:-.venv}
  [[ "$venv_dir" = /* ]] || venv_dir="$PROJECT_DIR/$venv_dir"
  if [[ -x "$venv_dir/bin/python" ]]; then export JOURNAL_PYTHON="$venv_dir/bin/python"; fi
fi
if [[ -z "${JOURNAL_PYTHON:-}" || ! -x "$JOURNAL_PYTHON" ]]; then
  echo "ERROR: set JOURNAL_PYTHON to the repo Python executable (for example .venv/bin/python)" >&2
  exit 2
fi
export JOURNAL_ENV_FILE=/dev/null
exec "$JOURNAL_PYTHON" -B "$PROJECT_DIR/src/Bootstrap_Matched_Evaluation_v2.py" "${@:-plan}"
