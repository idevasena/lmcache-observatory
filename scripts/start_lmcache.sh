#!/usr/bin/env bash
# Start the LMCache MP server configured for storage characterization.
#
#   ./10_start_lmcache.sh                          baseline
#   ./10_start_lmcache.sh --tracing                + OTel spans to Jaeger
#   ./10_start_lmcache.sh --trace-record FILE.lct  + storage trace recording
#   ./10_start_lmcache.sh --gds                    GDS backend instead of POSIX
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

L1_SIZE_GB="${L1_SIZE_GB:-100}"
CHUNK_SIZE="${CHUNK_SIZE:-256}"          # production default; NOT the docs' demo value of 16
L2_PATH="${L2_PATH:-/mnt/drives/nvme1n1/lmcache-l2}"
PROM_PORT="${PROM_PORT:-9095}"           # NOT 9090 -- that is Prometheus itself
ZMQ_PORT="${ZMQ_PORT:-5555}"
HTTP_PORT="${HTTP_PORT:-8080}"
BACKEND="POSIX"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tracing)
      EXTRA+=(--enable-tracing --otlp-endpoint "${OTLP:-http://localhost:4317}"); shift ;;
    --trace-record)
      mkdir -p "$(dirname "$2")"
      EXTRA+=(--trace-level storage --trace-output "$2"); shift 2 ;;
    --gds)     BACKEND="GDS"; shift ;;
    --gds-mt)  BACKEND="GDS_MT"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if ss -ltn "sport = :$PROM_PORT" | grep -q LISTEN; then
  echo "ERROR: port $PROM_PORT already in use. Pick another with PROM_PORT=..." >&2
  exit 1
fi

L2_ADAPTER=$(cat <<JSON
{"type":"nixl_store","backend":"$BACKEND","backend_params":{"file_path":"$L2_PATH","use_direct_io":"true"},"pool_size":128}
JSON
)

CMD=(lmcache server
  --host localhost --port "$ZMQ_PORT"
  --l1-size-gb "$L1_SIZE_GB"
  --eviction-policy LRU
  --chunk-size "$CHUNK_SIZE"
  --http-host 0.0.0.0 --http-port "$HTTP_PORT"
  --prometheus-port "$PROM_PORT"
  # 1.0, not the 0.01 default: every lifecycle histogram is sampled at this rate.
  # At 1% a short benchmark yields single-digit sample counts and noise p99s.
  --metrics-sample-rate 1.0
  # Default 10000 tail-drops under our burst rates; a dropped event silently
  # corrupts every histogram.
  --event-bus-queue-size 100000
  --l2-adapter "$L2_ADAPTER"
  "${EXTRA[@]}")

mkdir -p results
printf '%q ' "${CMD[@]}" > results/lmcache-cmdline.txt
echo >> results/lmcache-cmdline.txt
echo "--- launching ---"; cat results/lmcache-cmdline.txt; echo

LMCACHE_LOG_LEVEL="${LMCACHE_LOG_LEVEL:-INFO}" "${CMD[@]}" 2>&1 | tee results/lmcache-server.log &
sleep 8

echo
echo "--- verification ---"
curl -sf "http://localhost:$HTTP_PORT/health" && echo "  health OK" || echo "  health FAILED"
n=$(curl -sf "http://localhost:$PROM_PORT/metrics" | grep -c '^lmcache_mp' || echo 0)
echo "  lmcache_mp series exposed: $n"
[[ "$n" -gt 0 ]] || echo "  ERROR: no metrics. Check results/lmcache-server.log."
