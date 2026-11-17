#!/usr/bin/env bash
set -Eeuo pipefail

SOLVER="${1:-}"

case "$SOLVER" in
    cplex_mip|cplex_cp)
        ;;
    *)
        echo "Usage: $0 {cplex_mip|cplex_cp}"
        exit 2
        ;;
esac

TIMEOUT="${TIMEOUT:-7200}"
THREADS="${THREADS:-1}"
SEED="${SEED:-0}"
VERBOSE="${VERBOSE:-1}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# Kích hoạt đúng môi trường ảo.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    echo "Using active environment: $VIRTUAL_ENV"
elif [[ -f .venv/bin/activate ]]; then
    source .venv/bin/activate
elif [[ -f .venv-ubuntu/bin/activate ]]; then
    source .venv-ubuntu/bin/activate
else
    echo "ERROR: Không tìm thấy .venv hoặc .venv-ubuntu"
    exit 2
fi

echo "Python: $(which python)"

python - <<'PY'
import cplex
import docplex

engine = cplex.Cplex()
print("DOcplex version:", docplex.__version__)
print("CPLEX runtime:", engine.get_version())
PY

if [[ "$SOLVER" == "cplex_cp" ]]; then
    echo "CP Optimizer: $(command -v cpoptimizer)"
fi

OUTPUT_ROOT="${OUTPUT_ROOT:-output/${SOLVER}_official}"

DATASETS=(
    data_table03_origin
    data_table06_forb
    data_table07_fixed
    data_table08_prec
)

EXTRA_ARGS=()
if [[ "$VERBOSE" == "1" ]]; then
    EXTRA_ARGS+=(--verbose)
fi

mkdir -p "$OUTPUT_ROOT"

echo "============================================================"
echo "Solver       : $SOLVER"
echo "Timeout/run  : $TIMEOUT seconds"
echo "Threads      : $THREADS"
echo "Random seed  : $SEED"
echo "Output root  : $OUTPUT_ROOT"
echo "============================================================"

for DATA_DIR in "${DATASETS[@]}"; do
    OUT_DIR="$OUTPUT_ROOT/$DATA_DIR"

    if [[ -e "$OUT_DIR/detailed.csv" || -e "$OUT_DIR/aggregate.csv" ]]; then
        echo "ERROR: Kết quả đã tồn tại trong $OUT_DIR"
        echo "Không ghi đè kết quả cũ."
        exit 3
    fi

    mkdir -p "$OUT_DIR/excel"

    echo
    echo "============================================================"
    echo "Starting $SOLVER on $DATA_DIR"
    echo "Started UTC: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "============================================================"

    python src/Main.py \
        --data-dir "$DATA_DIR" \
        --solver "$SOLVER" \
        --timeout "$TIMEOUT" \
        --threads "$THREADS" \
        --random-seed "$SEED" \
        --csv "$OUT_DIR/aggregate.csv" \
        --long-csv "$OUT_DIR/detailed.csv" \
        --excel-dir "$OUT_DIR/excel" \
        "${EXTRA_ARGS[@]}" \
        2>&1 | tee "$OUT_DIR/run.log"

    echo "Finished $DATA_DIR at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
done

echo
echo "============================================================"
echo "COMPLETED: $SOLVER"
echo "Results: $OUTPUT_ROOT"
echo "============================================================"
