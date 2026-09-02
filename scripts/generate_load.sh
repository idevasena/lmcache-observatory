#!/usr/bin/env bash
# Generate shared-prefix traffic so the metric pipeline can be validated.
#
#   ./40_generate_load.sh smoke   two requests, proves the chain end to end
#   ./40_generate_load.sh bench   sustained load
#
# NOTE: this is for validating the OBSERVABILITY STACK, not for producing
# results. Synthetic uniform-prefix load has a reuse-gap distribution nothing
# like real agentic traffic -- drive the actual demos with the TensorMesh V3
# traces instead.
set -euo pipefail
MODE="${1:-smoke}"
VLLM="${VLLM:-http://localhost:8001}"
#MODEL="${MODEL:-meta-llama/Meta-Llama-3.1-8B-Instruct}"
MODEL="Qwen/Qwen2.5-7B-Instruct"

PREFIX=$(python3 -c "
import random; random.seed(7)
words='storage cache latency throughput device kernel buffer stream handle placement'.split()
print(' '.join(random.choice(words) for _ in range(1200)))")

send() {
  curl -s "$VLLM/v1/completions" -H 'Content-Type: application/json' \
    -d "$(python3 -c "
import json,sys
print(json.dumps({'model':'$MODEL','prompt':sys.argv[1]+' '+sys.argv[2],'max_tokens':16}))
" "$PREFIX" "$1")" -o /dev/null -w "  req '%{url_effective}' -> %{http_code} in %{time_total}s\n"
}

case "$MODE" in
  smoke)
    echo "Request 1 (cold -- expect 'Stored N tokens' in the lmcache log):"; send "alpha"
    sleep 2
    echo "Request 2 (warm -- expect 'Retrieved N tokens'):";                 send "beta"
    echo
    echo "Now check the counters moved:"
    echo "  curl -s localhost:9095/metrics | grep lookup_hit"
    ;;
  bench)
    DURATION="${DURATION:-300}"; CONC="${CONC:-8}"
    echo "Driving ${CONC} concurrent streams for ${DURATION}s..."
    end=$(( $(date +%s) + DURATION ))
    for i in $(seq 1 "$CONC"); do
      ( while [[ $(date +%s) -lt $end ]]; do send "stream$i-$RANDOM" >/dev/null; done ) &
    done
    wait
    echo "done"
    ;;
  *) echo "usage: $0 {smoke|bench}" >&2; exit 1 ;;
esac
