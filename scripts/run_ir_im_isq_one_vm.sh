#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

OUTPUT_ROOT=${OUTPUT_ROOT:-$PROJECT_DIR/outputs/journal}
MAIN_MERGED=${MAIN_OBJECTIVES_OUTPUT:-$OUTPUT_ROOT/main-objectives-merged}
MAIN_SHARD_0=${MAIN_OBJECTIVES_SHARD_0:-$OUTPUT_ROOT/main-objectives/shard-0-of-2}
MAIN_SHARD_1=${MAIN_OBJECTIVES_SHARD_1:-$OUTPUT_ROOT/main-objectives/shard-1-of-2}

if [[ ! -f "$MAIN_MERGED/plan.json" ]]; then
  bash scripts/run_journal_gcp.sh merge-shards \
    --input "$MAIN_SHARD_0" \
    --input "$MAIN_SHARD_1" \
    --output "$MAIN_MERGED" \
    --allow-dirty \
    --allow-mixed-environments
fi

export MAIN_OBJECTIVES_OUTPUT="$MAIN_MERGED"
export ALLOW_DIRTY=${ALLOW_DIRTY:-1}
export ALLOW_ENVIRONMENT_DRIFT=${ALLOW_ENVIRONMENT_DRIFT:-1}
unset SHARD_COUNT
unset SHARD_INDICES

bash scripts/run_journal_gcp.sh final-tier
