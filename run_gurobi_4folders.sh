#!/usr/bin/env bash

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
OUT="$ROOT/output/gurobi"

TIMEOUT="${TIMEOUT:-7200}"
THREADS="${THREADS:-1}"
SEED="${SEED:-0}"

export GRB_LICENSE_FILE="${GRB_LICENSE_FILE:-$HOME/gurobi.lic}"
export PYTHONUNBUFFERED=1

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python virtual environment not found: $PYTHON"
    exit 1
fi

if [[ ! -f "$GRB_LICENSE_FILE" ]]; then
    echo "ERROR: Gurobi license not found: $GRB_LICENSE_FILE"
    exit 1
fi

# Mỗi lần chạy script sẽ tạo bộ kết quả hoàn toàn mới.
rm -rf -- "$OUT"
mkdir -p "$OUT"

run_folder() {
    local code="$1"
    local data_dir="$2"

    local aggregate="$OUT/${code}_aggregate.csv"
    local detailed="$OUT/${code}_detailed.csv"
    local excel_dir="$OUT/${code}_excel"
    local log_file="$OUT/${code}.log"
    local exit_file="$OUT/${code}_exit_code.txt"

    mkdir -p "$excel_dir"

    echo
    echo "============================================================"
    echo "Starting $code"
    echo "Data directory : $data_dir"
    echo "Started UTC    : $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "Threads        : $THREADS"
    echo "Timeout        : $TIMEOUT seconds per instance"
    echo "============================================================"

    set +e

    "$PYTHON" -u "$ROOT/src/Main.py" \
        --data-dir "$ROOT/$data_dir" \
        --solver gurobi_mip \
        --timeout "$TIMEOUT" \
        --threads "$THREADS" \
        --random-seed "$SEED" \
        --csv "$aggregate" \
        --long-csv "$detailed" \
        --excel-dir "$excel_dir" \
        --verbose \
        2>&1 | tee "$log_file"

    local run_status=${PIPESTATUS[0]}

    set -e

    printf '%s\n' "$run_status" > "$exit_file"

    if [[ "$run_status" -ne 0 ]]; then
        echo
        echo "ERROR: $code stopped with exit code $run_status"
        echo "Inspect: $log_file"
        exit "$run_status"
    fi

    echo
    echo "Completed $code successfully"
    echo "Finished UTC: $(date -u '+%Y-%m-%d %H:%M:%S')"
}

echo "============================================================"
echo "Sequential Gurobi benchmark"
echo "Project : $ROOT"
echo "Output  : $OUT"
echo "License : $GRB_LICENSE_FILE"
echo "============================================================"

run_folder "table03" "data_table03_origin"
run_folder "table06" "data_table06_forb"
run_folder "table07" "data_table07_fixed"
run_folder "table08" "data_table08_prec"

touch "$OUT/COMPLETED"

echo
echo "============================================================"
echo "ALL FOUR FOLDERS COMPLETED"
echo "Finished UTC: $(date -u '+%Y-%m-%d %H:%M:%S')"
echo "Output: $OUT"
echo "============================================================"
