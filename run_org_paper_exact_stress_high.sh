#!/usr/bin/env bash

cd /home/g24021555/Business-Meeting-Scheduling-Problem || exit 1
source .venv/bin/activate

export UWRMAXSAT_BIN=/home/g24021555/uwrmaxsat/build/release/bin/uwrmaxsat
export UWRMAXSAT_SHA256=8c68c8a386d3d7847b50d315143c2bab60e3514e59510e84a649db83cb303876

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTHONHASHSEED=0

D=data_precedence_stress_high
OUT=output/org_paper_exact/session3_precedence_stress_high

mkdir -p "$OUT"

while IFS= read -r INSTANCE; do

    NAME=$(basename "$INSTANCE" .dzn)

    echo
    echo "############################################################"
    echo "RUNNING INSTANCE: $INSTANCE"
    echo "INSTANCE NAME   : $NAME"
    echo "DATASET         : $D"
    echo "TIMEOUT         : 7200"
    echo "MODEL           : ORG_PAPER_EXACT"
    echo "OBJECTIVE       : BG-d2"
    echo "SOLVER          : UWrMaxSAT"
    echo "DOMAIN          : Full"
    echo "PRECEDENCE      : Pairwise + Direct-E"
    echo "IC              : IC12"
    echo "############################################################"

    python -u src/ORG_PAPER_EXACT.py \
        --instance "$INSTANCE" \
        --family all \
        --backend uwrmaxsat \
        --uwrmaxsat-bin "$UWRMAXSAT_BIN" \
        --uwrmaxsat-sha256 "$UWRMAXSAT_SHA256" \
        --timeout 7200 \
        --csv "$OUT/${NAME}.csv" \
        --excel-dir "$OUT/excel"

    STATUS=$?

    echo
    echo "FINISHED INSTANCE: $NAME"
    echo "EXIT CODE       : $STATUS"
    echo "------------------------------------------------------------"

done < <(
    PYTHONPATH=src python - "$D" <<'PY'
import sys
from Main import collect_instances

directory = sys.argv[1]

for spec in collect_instances(
    None,
    directory,
    None,
    "all",
):
    print(spec.path)
PY
)

echo
echo "============================================================"
echo "SESSION 3 FINISHED"
echo "============================================================"
