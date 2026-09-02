#!/usr/bin/env bash
# Reconcile dashboard metric names against what the server actually exports.
#
# The OTel Prometheus exporter appends unit suffixes that are NOT uniform across
# metrics (_chunks_total, _requests_total, _tokens_total, _GB_per_second, _seconds).
# A mismatch renders a panel as "No Data" rather than erroring, which looks exactly
# like a workload problem. Run this BEFORE generating load.
#
#   ./30_verify_metrics.sh              reconcile names + check dashboard queries
#   ./30_verify_metrics.sh --post-run   run the three validity checks
set -uo pipefail
cd "$(dirname "$0")/.."

PROM="${PROM:-http://localhost:9090}"
LMC="${LMC:-http://localhost:8080}"
mkdir -p results

q() {  # promql -> series count
  curl -sfG "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
    | python3 -c "import sys,json
try: print(len(json.load(sys.stdin)['data']['result']))
except Exception: print(-1)" 2>/dev/null || echo -1
}

qv() { # promql -> scalar value
  curl -sfG "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
    | python3 -c "import sys,json
try:
    r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')
except Exception: print('ERR')" 2>/dev/null || echo ERR
}

if [[ "${1:-}" == "--post-run" ]]; then
  echo "=== Run validity $(date -u +%FT%TZ) ==="
  d=$(qv 'sum(lmcache_mp_event_bus_dropped_events_total)')
  f=$(qv 'sum(lmcache_mp_l1_read_failure_total)')
  s=$(grep -o -- '--metrics-sample-rate [0-9.]*' results/lmcache-cmdline.txt 2>/dev/null || echo 'unknown')
  printf 'eventbus_dropped: %s   %s\n' "$d" "$([[ "$d" == "0" ]] && echo PASS || echo '*** FAIL: histograms invalid, re-run with larger --event-bus-queue-size ***')"
  printf 'l1_read_failure:  %s   %s\n' "$f" "$([[ "$f" == "0" ]] && echo PASS || echo '*** FAIL: lookup/reserve race or unexpected eviction, run is void ***')"
  printf 'sample_rate:      %s   %s\n' "$s" "$([[ "$s" == *"1.0"* ]] && echo PASS || echo '*** WARN: histograms are subsampled ***')"
  { echo "=== $(date -u +%FT%TZ) ==="; echo "dropped=$d read_failure=$f $s"; } >> results/run-validity.txt
  exit 0
fi

echo "=================================================================="
echo " 1. Metric names actually exported by the LMCache MP server"
echo "=================================================================="
curl -sf "$LMC/metrics" > results/mp_metrics_raw.txt || { echo "cannot reach $LMC"; exit 1; }
grep '^# TYPE' results/mp_metrics_raw.txt | awk '{print $3"\t"$4}' | sort > results/mp_metrics_types.txt
echo "exported series: $(wc -l < results/mp_metrics_types.txt)"
echo
echo "by subsystem:"
awk '{print $1}' results/mp_metrics_types.txt | sed 's/^lmcache_mp_//' \
  | cut -d_ -f1 | sort | uniq -c | sort -rn | sed 's/^/  /'

echo
echo "=================================================================="
echo " 2. Do the dashboard's metric names exist?"
echo "=================================================================="
MISSING=0
while read -r name; do
  [[ -z "$name" ]] && continue
  # a histogram appears as _bucket/_sum/_count; a counter as itself
  if grep -qE "^${name}(_bucket|_sum|_count)?[[:space:]]" results/mp_metrics_types.txt; then
    printf '  %-58s OK\n' "$name"
  elif [[ "$name" == vllm:* || "$name" == DCGM_* ]]; then
    n=$(q "$name")
    if [[ "$n" -gt 0 ]]; then printf '  %-58s OK (external, %s series)\n' "$name" "$n"
    else printf '  %-58s MISSING (external exporter down?)\n' "$name"; MISSING=$((MISSING+1)); fi
  else
    printf '  %-58s *** NOT FOUND ***\n' "$name"
    MISSING=$((MISSING+1))
    # suggest the closest real name
    stem=$(echo "$name" | sed -E 's/^lmcache_mp_//; s/_(total|bucket|sum|count|seconds|bytes)$//')
    cand=$(grep -oE "lmcache_mp_[a-zA-Z0-9_]*${stem}[a-zA-Z0-9_]*" results/mp_metrics_types.txt | sort -u | head -3)
    [[ -n "$cand" ]] && echo "$cand" | sed 's/^/        did you mean: /'
  fi
done < scripts/dashboard_metrics.txt

echo
echo "=================================================================="
echo " 3. Prometheus scrape targets"
echo "=================================================================="
curl -sf "$PROM/api/v1/targets" | python3 -c "
import sys,json
try: t=json.load(sys.stdin)['data']['activeTargets']
except Exception: print('  cannot reach Prometheus'); sys.exit()
for x in t:
    print(f\"  {x['labels']['job']:<12} {x['health']:<8} {x['scrapeUrl']}\")
    if x['health']!='up': print('     error:', x.get('lastError'))
"

echo
echo "=================================================================="
echo " 4. Recording rules producing data?"
echo "=================================================================="
for r in lmcache:token_hit_rate lmcache:is_healthy lmcache:l2_store_iops \
         lmcache:l2_load_iops lmcache:l2_read_write_inflight_ratio \
         lmcache:slow_l2_load_ratio; do
  printf '  %-42s %s series\n' "$r" "$(q "$r")"
done

echo
if [[ "$MISSING" -gt 0 ]]; then
  echo "*** $MISSING metric name(s) did not resolve. ***"
  echo "Fix the M{} dict in scripts/gen_dashboard.py using the suggestions above,"
  echo "then:  python3 scripts/gen_dashboard.py && docker restart grafana"
  exit 1
fi
echo "All dashboard metric names resolved. Safe to generate load."
