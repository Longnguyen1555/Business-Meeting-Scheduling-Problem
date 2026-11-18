#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

if [[ -f "${JOURNAL_ENV_FILE:-$PROJECT_DIR/journal_gcp.env}" ]]; then
  # shellcheck disable=SC1090
  source "${JOURNAL_ENV_FILE:-$PROJECT_DIR/journal_gcp.env}"
fi

JOURNAL_VENV=${JOURNAL_VENV:-.venv-ubuntu}
if [[ ! -d "$JOURNAL_VENV" ]]; then
  if [[ "${BOOTSTRAP_VENV:-0}" != "1" ]]; then
    echo "ERROR: missing $JOURNAL_VENV (set BOOTSTRAP_VENV=1 to create it)" >&2
    exit 2
  fi
  python3 -m venv "$JOURNAL_VENV"
  # This is the only network-dependent step and is explicit.
  "$JOURNAL_VENV/bin/python" -m pip install --upgrade pip
  "$JOURNAL_VENV/bin/python" -m pip install -r src/requirements.txt
fi

# shellcheck disable=SC1090
source "$JOURNAL_VENV/bin/activate"

if [[ -z "${UWRMAXSAT_BIN:-}" || ! -x "${UWRMAXSAT_BIN:-}" ]]; then
  echo "ERROR: UWRMAXSAT_BIN must name the executable used for production" >&2
  exit 2
fi
if [[ ! "${UWRMAXSAT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "ERROR: UWRMAXSAT_SHA256 must be a pinned 64-digit lowercase hash" >&2
  exit 2
fi

actual_hash=$(sha256sum "$UWRMAXSAT_BIN" | awk '{print $1}')
if [[ "$actual_hash" != "$UWRMAXSAT_SHA256" ]]; then
  echo "ERROR: UWrMaxSAT hash mismatch: $actual_hash" >&2
  exit 2
fi

PYTHONPATH=src python - <<'PY'
import json
import platform
import psutil
from pysat.solvers import Solver
from Journal_Experiment import current_machine_profile, machine_profile_errors

with Solver(name="cadical153", bootstrap_with=[[1]]) as solver:
    assert solver.solve()
with open("journal_configs/official_core.json", encoding="utf-8") as stream:
    required = json.load(stream)["required_machine"]
actual = current_machine_profile()
print("python", platform.python_version())
print("platform", platform.platform())
print("cpu_model", actual["cpu_model"])
print("logical_cpus", actual["logical_cpu_cores"])
print("physical_cpus", actual["physical_cpu_cores"])
print("memory_gib", round(actual["system_memory_mb"] / 1024, 2))
print("swap_mib", actual["swap_memory_mb"])
errors = machine_profile_errors(required, actual)
if errors:
    raise SystemExit("conference machine profile mismatch: " + "; ".join(errors))
print("CaDiCaL smoke: OK")
print("conference machine profile: OK")
PY

python -m py_compile \
  src/Main.py \
  src/ORG_BG_D2.py \
  src/Audit_Journal_Coverage.py \
  src/Journal_Experiment.py \
  src/Journal_Instance_Features.py \
  src/Generate_Journal_Benchmark.py \
  src/Validate_Journal_Run.py

if [[ "${RUN_TESTS:-1}" == "1" ]]; then
  python -m unittest discover -s tests -v
fi

echo "uwrmaxsat=$UWRMAXSAT_BIN"
echo "uwrmaxsat_sha256=$actual_hash"
echo "git_commit=$(git rev-parse HEAD)"
git status --short
mkdir -p "${OUTPUT_ROOT:-$PROJECT_DIR/outputs/journal}"
df -h "${OUTPUT_ROOT:-$PROJECT_DIR/outputs/journal}"
echo "GCP journal environment preflight: OK"
