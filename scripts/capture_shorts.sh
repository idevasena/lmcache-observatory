#!/usr/bin/env bash
# Render the agreed shot list to shots/ via the Grafana image-renderer.
#
# Requires:  docker exec -it grafana grafana cli plugins install grafana-image-renderer
#            docker restart grafana
#            export GRAFANA_TOKEN=<service account token>
#
# Always renders with tz=UTC and an ABSOLUTE time range -- relative ranges make
# screenshots from different demo runs silently incomparable.
set -euo pipefail
cd "$(dirname "$0")/.."

GRAFANA="${GRAFANA:-http://localhost:3000}"
UID_DASH="${UID_DASH:-lmcache-mp-tiers}"
SLUG="${SLUG:-lmcache-mp-tiers}"
TOKEN="${GRAFANA_TOKEN:?set GRAFANA_TOKEN to a Grafana service-account token}"
TAG="${TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"

# Absolute window in epoch ms. Override FROM_MS/TO_MS to re-render a past run.
TO_MS="${TO_MS:-$(( $(date +%s) * 1000 ))}"
FROM_MS="${FROM_MS:-$(( TO_MS - 1800000 ))}"     # default: last 30 min

OUT="shots/$TAG"; mkdir -p "$OUT"
echo "rendering window $(date -u -d @$((FROM_MS/1000)) +%FT%TZ) .. $(date -u -d @$((TO_MS/1000)) +%FT%TZ)"

render() {  # name panelId(optional) width height
  local name="$1" pid="${2:-}" w="${3:-1200}" h="${4:-600}" url
  if [[ -n "$pid" ]]; then
    url="$GRAFANA/render/d-solo/$UID_DASH/$SLUG?orgId=1&panelId=$pid"
  else
    url="$GRAFANA/render/d/$UID_DASH/$SLUG?orgId=1&kiosk"
  fi
  url="$url&from=$FROM_MS&to=$TO_MS&width=$w&height=$h&tz=UTC"
  if curl -sf -H "Authorization: Bearer $TOKEN" "$url" -o "$OUT/$name.png"; then
    printf '  %-40s %s\n' "$name.png" "$(du -h "$OUT/$name.png" | cut -f1)"
  else
    printf '  %-40s FAILED\n' "$name.png"
  fi
}

# Panel ids are assigned sequentially by gen_dashboard.py. Resolve them by title
# so the shot list survives dashboard edits.
mapfile -t IDS < <(python3 - <<'PY'
import json
d=json.load(open('grafana/dashboards/lmcache-mp-tiers.json'))
want=["Token hit rate","TTFT (vLLM)","L1 usage ratio vs eviction watermark",
      "L2 occupancy by backend","L1\u2192L2 store throughput (submit\u2192complete)",
      "L2\u2192L1 load throughput (submit\u2192complete)",
      "In-flight L2 stores vs loads","L1 chunk lifetime","Real reuse gap (time)",
      "Failure counters","EventBus health"]
byt={p.get('title'):p['id'] for p in d['panels']}
for w in want: print(f"{w}\t{byt.get(w,'')}")
PY
)

echo "01 overview"; render "01_overview" "" 1600 2400
i=2
for row in "${IDS[@]}"; do
  title="${row%%$'\t'*}"; pid="${row##*$'\t'}"
  [[ -z "$pid" ]] && { echo "  skip (panel not found): $title"; continue; }
  slug=$(echo "$title" | tr '[:upper:] ' '[:lower:]_' | tr -cd 'a-z0-9_')
  render "$(printf '%02d' $i)_$slug" "$pid" 1200 550
  i=$((i+1))
done

# Provenance sidecar -- without this a screenshot is unfalsifiable.
{ echo "captured: $(date -u +%FT%TZ)"
  echo "window:   $FROM_MS .. $TO_MS (epoch ms, UTC)"
  echo "dashboard: $UID_DASH"
  cat results/environment.txt 2>/dev/null
  cat results/lmcache-cmdline.txt 2>/dev/null
} > "$OUT/PROVENANCE.txt"

echo; echo "shots in $OUT (with PROVENANCE.txt)"
