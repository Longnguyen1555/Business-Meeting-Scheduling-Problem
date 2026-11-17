#!/usr/bin/env bash

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

TIMEOUT="${TIMEOUT:-7200}"
RUN_ID="${RUN_ID:-official-$(date -u +%Y%m%dT%H%M%SZ)}"

UWRMAXSAT_BIN="${UWRMAXSAT_BIN:-$HOME/solver-build/uwrmaxsat/build/release/bin/uwrmaxsat}"
MANIFEST="${MANIFEST:-$ROOT/instances_manifest.csv}"
NOVES_DIR="${NOVES_DIR:-$ROOT/../noves}"

# Tất cả nhóm lấy theo canonical manifest. Manifest tách đúng family và gom
# 14 cặp path alias của Forbidden thành 26 nội dung cần giải.
DATASET_SPECS=(
  "data_table03_origin|original"
  "data_table06_forb|forbidden"
  "data_table07_fixed|fixed"
  "data_table08_prec|precedence"
)

# Kích hoạt môi trường Python.
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
    source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/.venv-ubuntu/bin/activate" ]]; then
    source "$ROOT/.venv-ubuntu/bin/activate"
else
    echo "ERROR: không tìm thấy .venv hoặc .venv-ubuntu."
    exit 1
fi

# Kiểm tra source và dữ liệu.
if [[ ! -f "$ROOT/src/Main.py" || ! -f "$ROOT/src/ORG_new.py" ]]; then
    echo "ERROR: script phải được đặt tại thư mục gốc repository."
    exit 1
fi

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: không tồn tại canonical manifest:"
    echo "  $MANIFEST"
    exit 1
fi

if [[ ! -d "$NOVES_DIR" ]]; then
    echo "ERROR: không tồn tại official noves directory:"
    echo "  $NOVES_DIR"
    exit 1
fi

forbidden_path_count="$(
    find "$NOVES_DIR" -maxdepth 1 -type f \
        \( -name '*.forb0003.dzn' -o -name '*.forb0007.dzn' \) \
        | wc -l | tr -d ' '
)"
if [[ "$forbidden_path_count" != "40" ]]; then
    echo "ERROR: noves phải chứa đúng 40 official Forbidden paths;"
    echo "tìm thấy $forbidden_path_count."
    exit 1
fi

# Kiểm tra UWrMaxSAT.
if [[ ! -x "$UWRMAXSAT_BIN" ]]; then
    echo "ERROR: không tìm thấy executable:"
    echo "  $UWRMAXSAT_BIN"
    exit 1
fi

export UWRMAXSAT_BIN
UWRMAXSAT_SHA256="$(sha256sum "$UWRMAXSAT_BIN" | awk '{print $1}')"
export UWRMAXSAT_SHA256

OUT="$ROOT/output/ic12p-$RUN_ID"

mkdir -p \
    "$OUT/logs" \
    "$OUT/main" \
    "$OUT/org"

# Lưu thông tin lần chạy.
{
    echo "run_id=$RUN_ID"
    echo "started_utc=$(date -u --iso-8601=seconds)"
    echo "root=$ROOT"
    echo "timeout_per_configuration=$TIMEOUT"
    echo "main_encoding_variant=imp12+"
    echo "main_solver=sat_all"
    echo "main_domain_mode=both"
    echo "main_sat_backend=cadical"
    echo "main_maxsat_backend=uwrmaxsat"
    echo "input_mode=canonical_manifest_content_deduplicated"
    echo "manifest=$MANIFEST"
    echo "manifest_sha256=$(sha256sum "$MANIFEST" | awk '{print $1}')"
    echo "noves_dir=$NOVES_DIR"
    echo "forbidden_path_count=$forbidden_path_count"
    echo "expected_official_paths=140"
    echo "expected_unique_contents=126"
    echo "expected_org_rows=126"
    echo "expected_main_rows=1476"
    echo "expected_logical_main_path_cells=1560"
    echo "uwrmaxsat_bin=$UWRMAXSAT_BIN"
    echo "uwrmaxsat_sha256=$UWRMAXSAT_SHA256"
    echo "git_commit=$(git rev-parse HEAD 2>/dev/null || true)"
    echo
    echo "=== GIT STATUS ==="
    git status --short 2>/dev/null || true
    echo
    echo "=== CPU ==="
    lscpu
    echo
    echo "=== MEMORY ==="
    free -h
    echo
    echo "=== DISK ==="
    df -h "$ROOT"
} > "$OUT/environment.txt"

printf "stage\tfolder\texit_code\tfinished_utc\n" > "$OUT/status.tsv"

run_stage() {
    local stage="$1"
    local folder="$2"
    shift 2

    local log_file="$OUT/logs/${stage}_${folder}.log"

    echo
    echo "============================================================"
    echo "START:  $stage / $folder"
    echo "UTC:    $(date -u --iso-8601=seconds)"
    echo "LOG:    $log_file"
    echo "============================================================"

    "$@" 2>&1 | tee "$log_file"
    local rc=${PIPESTATUS[0]}

    printf "%s\t%s\t%s\t%s\n" \
        "$stage" \
        "$folder" \
        "$rc" \
        "$(date -u --iso-8601=seconds)" \
        >> "$OUT/status.tsv"

    if (( rc != 0 )); then
        echo
        echo "ERROR: $stage / $folder kết thúc với exit code $rc"
        echo "Xem log:"
        echo "  $log_file"
        return "$rc"
    fi

    echo "COMPLETED: $stage / $folder"
    return 0
}

echo "Output directory: $OUT"
echo "Timeout: $TIMEOUT giây cho mỗi configuration"
echo "UWrMaxSAT: $UWRMAXSAT_BIN"

# ============================================================
# Giai đoạn 1: ORG_new baseline
# Một run cho mỗi canonical instance.
# ============================================================

for dataset_spec in "${DATASET_SPECS[@]}"; do
    IFS='|' read -r data_dir family <<< "$dataset_spec"
    input_args=(--manifest "$MANIFEST" --family "$family")
    run_stage "org" "$data_dir" \
        python -u src/ORG_new.py \
            "${input_args[@]}" \
            --timeout "$TIMEOUT" \
            --uwrmaxsat-bin "$UWRMAXSAT_BIN" \
            --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256" \
            --csv "$OUT/org/${data_dir}_org_new.csv" \
            --excel-dir "$OUT/org/excel_${data_dir}"

    rc=$?
    if (( rc != 0 )); then
        exit "$rc"
    fi
done

# ============================================================
# Giai đoạn 2: Main — chỉ IC12+
#
# Không truyền precedence flags:
# - instance không precedence: pairwise + direct
# - instance có precedence: đủ bốn P×G
# ============================================================

for dataset_spec in "${DATASET_SPECS[@]}"; do
    IFS='|' read -r data_dir family <<< "$dataset_spec"
    input_args=(--manifest "$MANIFEST" --family "$family")
    run_stage "main" "$data_dir" \
        python -u src/Main.py \
            "${input_args[@]}" \
            --solver sat_all \
            --maxsat-backend uwrmaxsat \
            --uwrmaxsat-bin "$UWRMAXSAT_BIN" \
            --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256" \
            --sat-backend cadical \
            --domain-mode both \
            --encoding-variant 'imp12+' \
            --timeout "$TIMEOUT" \
            --csv "$OUT/main/${data_dir}_aggregate.csv" \
            --long-csv "$OUT/main/${data_dir}_detailed.csv" \
            --excel-dir "$OUT/main/excel_${data_dir}" \
            --verbose

    rc=$?
    if (( rc != 0 )); then
        exit "$rc"
    fi
done

run_stage "validation" "official_matrix" \
    python -u src/Validate_Official_Run.py \
        --output "$OUT" \
        --noves-dir "$NOVES_DIR"
rc=$?
if (( rc != 0 )); then
    echo "ERROR: kết quả không đạt ma trận benchmark chính thức."
    exit "$rc"
fi

echo "finished_utc=$(date -u --iso-8601=seconds)" \
    >> "$OUT/environment.txt"

tar \
    -C "$ROOT/output" \
    -czf "$ROOT/output/ic12p-$RUN_ID.tar.gz" \
    "ic12p-$RUN_ID"

echo
echo "============================================================"
echo "BENCHMARK HOÀN THÀNH"
echo "Results: $OUT"
echo "Archive: $ROOT/output/ic12p-$RUN_ID.tar.gz"
echo "============================================================"
