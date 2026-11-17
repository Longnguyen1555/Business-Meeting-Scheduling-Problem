#!/usr/bin/env bash

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

TIMEOUT="${TIMEOUT:-7200}"
RUN_ID="${RUN_ID:-filter-e-all-$(date -u +%Y%m%dT%H%M%SZ)}"
PLAN_ONLY="${PLAN_ONLY:-0}"
UWRMAXSAT_BIN="${UWRMAXSAT_BIN:-$HOME/solver-build/uwrmaxsat/build/release/bin/uwrmaxsat}"

DATASET_SPECS=(
    "official|$ROOT/instances_manifest.csv|40|480"
    "stress|$ROOT/data_precedence_stress/instances_manifest.csv|60|720"
    "stress-high|$ROOT/data_precedence_stress_high/instances_manifest.csv|40|480"
)

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
    source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/.venv-ubuntu/bin/activate" ]]; then
    source "$ROOT/.venv-ubuntu/bin/activate"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: không tìm thấy Python executable: $PYTHON_BIN"
    exit 1
fi

"$PYTHON_BIN" -u src/Validate_Filter_E_Run.py --check-manifests-only
rc=$?
if (( rc != 0 )); then
    exit "$rc"
fi

if [[ "$PLAN_ONLY" == "1" ]]; then
    echo "PLAN_ONLY=1: không khởi chạy solver."
    exit 0
fi

if [[ ! -x "$UWRMAXSAT_BIN" ]]; then
    echo "ERROR: không tìm thấy UWrMaxSAT executable:"
    echo "  $UWRMAXSAT_BIN"
    exit 1
fi

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

export UWRMAXSAT_BIN
UWRMAXSAT_SHA256="$(sha256_file "$UWRMAXSAT_BIN")"
export UWRMAXSAT_SHA256

OUT="$ROOT/output/$RUN_ID"
mkdir -p "$OUT/logs" "$OUT/main"

{
    echo "run_id=$RUN_ID"
    echo "started_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "root=$ROOT"
    echo "timeout_per_configuration=$TIMEOUT"
    echo "matrix=Reduced x Filter-E x 2P x 2G x 3S x IC12+"
    echo "datasets=official-precedence,stress,stress-high"
    echo "expected_instances=140"
    echo "expected_configurations_per_instance=12"
    echo "expected_filter_e_rows=1680"
    echo "official_expected_rows=480"
    echo "stress_expected_rows=720"
    echo "stress_high_expected_rows=480"
    echo "maxsat_backend=uwrmaxsat"
    echo "sat_backend=cadical"
    echo "uwrmaxsat_bin=$UWRMAXSAT_BIN"
    echo "uwrmaxsat_sha256=$UWRMAXSAT_SHA256"
    echo "git_commit=$(git rev-parse HEAD 2>/dev/null || true)"
    echo
    echo "=== GIT STATUS ==="
    git status --short 2>/dev/null || true
} > "$OUT/environment.txt"

printf "dataset\tmanifest\texpected_instances\texpected_rows\texit_code\tfinished_utc\n" \
    > "$OUT/status.tsv"

run_dataset() {
    local dataset="$1"
    local manifest="$2"
    local expected_instances="$3"
    local expected_rows="$4"
    local log_file="$OUT/logs/${dataset}.log"

    echo
    echo "============================================================"
    echo "START: $dataset"
    echo "MANIFEST: $manifest"
    echo "EXPECTED: $expected_instances instances, $expected_rows Filter-E rows"
    echo "LOG: $log_file"
    echo "============================================================"

    "$PYTHON_BIN" -u src/Main.py \
        --manifest "$manifest" \
        --family precedence \
        --solver sat_all \
        --maxsat-backend uwrmaxsat \
        --uwrmaxsat-bin "$UWRMAXSAT_BIN" \
        --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256" \
        --sat-backend cadical \
        --domain-mode reduced \
        --domain-filter-graph direct \
        --precedence-encoding both \
        --precedence-graph both \
        --encoding-variant 'imp12+' \
        --timeout "$TIMEOUT" \
        --csv "$OUT/main/${dataset}_aggregate.csv" \
        --long-csv "$OUT/main/${dataset}_detailed.csv" \
        --excel-dir "$OUT/main/excel_${dataset}" \
        --verbose \
        2>&1 | tee "$log_file"
    local stage_rc=${PIPESTATUS[0]}

    printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$dataset" \
        "$manifest" \
        "$expected_instances" \
        "$expected_rows" \
        "$stage_rc" \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        >> "$OUT/status.tsv"

    return "$stage_rc"
}

echo "Output directory: $OUT"
echo "Filter-E run count: 1680"

for dataset_spec in "${DATASET_SPECS[@]}"; do
    IFS='|' read -r dataset manifest expected_instances expected_rows \
        <<< "$dataset_spec"
    run_dataset "$dataset" "$manifest" "$expected_instances" "$expected_rows"
    rc=$?
    if (( rc != 0 )); then
        echo "ERROR: dataset $dataset kết thúc với exit code $rc"
        exit "$rc"
    fi
done

"$PYTHON_BIN" -u src/Validate_Filter_E_Run.py --output "$OUT" \
    2>&1 | tee "$OUT/logs/validation.log"
rc=${PIPESTATUS[0]}
if (( rc != 0 )); then
    echo "ERROR: kết quả không đạt ma trận Filter-E 1680 run."
    exit "$rc"
fi

echo "finished_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    >> "$OUT/environment.txt"

tar \
    -C "$ROOT/output" \
    -czf "$ROOT/output/$RUN_ID.tar.gz" \
    "$RUN_ID"

echo
echo "============================================================"
echo "FILTER-E BENCHMARK HOÀN THÀNH"
echo "Results: $OUT"
echo "Archive: $ROOT/output/$RUN_ID.tar.gz"
echo "============================================================"
