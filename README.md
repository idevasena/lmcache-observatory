# LMCache MP Observability Bring-Up on a Single GPU Server

Runbook for standing up LMCache 0.5.4 in MP mode with a full Prometheus + Grafana
observability stack on one GPU host (DGX 8×H100 or equivalent). Bare-metal /
docker-compose, not Kubernetes.

Additional information to LMCache Observability page. This runbook assumes you already know why we're on MP mode and not IP mode.

---

## 0. Port map

The single most common failure in this stack is a port collision, because LMCache's
Prometheus endpoint and Prometheus itself both default to 9090. This bundle moves
LMCache to 9095 everywhere. If you deviate, change it in `prometheus/prometheus.yml`
and `scripts/start_lmcache.sh` together.

| Port | Service | Notes |
|---|---|---|
| 5555 | LMCache ZMQ | vLLM engine connections |
| 8080 | LMCache HTTP management API | `/health`, cache management |
| **9095** | **LMCache Prometheus `/metrics`** | **moved off the 9090 default** |
| 8001 | vLLM OpenAI server | also serves `vllm:*` metrics at `/metrics` |
| 9090 | Prometheus | |
| 3000 | Grafana | admin / admin on first login |
| 9100 | node_exporter | host DRAM, CPU, disk |
| 9400 | DCGM exporter | GPU HBM, utilization, power |
| 4317 | Jaeger OTLP gRPC | optional, tracing only |
| 16686 | Jaeger UI | optional |

Check them all before you start:

```bash
for p in 3000 4317 5555 8001 8080 9090 9095 9100 9400 16686; do
  ss -ltn "sport = :$p" | grep -q LISTEN && echo "PORT $p BUSY" || echo "port $p free"
done
```

---

## 1. Host prerequisites

```bash
# GPU sanity — confirm the driver sees all 8 devices
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv

# On the DGX, note that Memory-Usage may report oddly under unified memory.
# Don't rely on nvidia-smi for HBM occupancy here — that's what DCGM is for (§4).

# Docker + NVIDIA container toolkit
docker --version
docker run --rm --gpus all ubuntu nvidia-smi -L

# The NVMe device under test, and its current queue limits.
# Capture these NOW — they go in the results file for every run.
lsblk -d -o NAME,SIZE,MODEL,ROTA
nvme list
nvme id-ctrl /dev/nvme0 | grep -iE 'mdts|mn |sn '
cat /sys/block/nvme0n1/queue/max_hw_sectors_kb
cat /sys/block/nvme0n1/queue/max_sectors_kb
cat /sys/block/nvme0n1/queue/scheduler
cat /sys/block/nvme0n1/queue/nr_requests
```

Create the L2 landing directory on the NVMe under test:

```bash
sudo mkdir -p /data/nvme/lmcache-l2
sudo chown -R $USER:$USER /data/nvme/lmcache-l2
df -h /data/nvme/lmcache-l2       # confirm it's on the device you think it is
findmnt -no SOURCE /data/nvme     # confirm the backing block device
```

That last check matters more than it looks. Landing L2 on the OS drive by accident
produces a perfectly plausible-looking run that measures nothing.

---

## 2. Install LMCache and vLLM

```bash
mkdir -p ~/lmcache-obs && cd ~/lmcache-obs
uv venv --python 3.12
source .venv/bin/activate

# NIXL became an optional extra as of 0.5.2 — you need it for the GDS/POSIX
# L2 adapters, which is the whole point for storage work.
uv pip install "lmcache[nixl]==0.5.4"
uv pip install vllm

uv run python - <<'EOF'
import lmcache, sys
print("lmcache", lmcache.__version__)
try:
    import vllm; print("vllm", vllm.__version__)
except Exception as e:
    print("vllm import failed:", e)
print("python", sys.version.split()[0])
EOF
```

Record that block verbatim into `results/environment.txt`. Every number the demos
produce is only meaningful against a stated version pair.

```bash
mkdir -p results
{ echo "=== $(date -u +%FT%TZ) ==="; hostname; nvidia-smi -L
  python -c "import lmcache;print('lmcache',lmcache.__version__)"
  python -c "import vllm;print('vllm',vllm.__version__)"
  nvme id-ctrl /dev/nvme0 | grep -i mdts
  cat /sys/block/nvme0n1/queue/max_sectors_kb
  cat /sys/block/nvme0n1/queue/scheduler
} > results/environment.txt
cat results/environment.txt
```

---

## 3. Start the LMCache MP server

Use `scripts/start_lmcache.sh`. The important parts, and why:

```bash
lmcache server \
  --host localhost --port 5555 \
  --l1-size-gb 100 \
  --eviction-policy LRU \
  --chunk-size 256 \
  --http-host 0.0.0.0 --http-port 8080 \
  --prometheus-port 9095 \
  --metrics-sample-rate 1.0 \
  --event-bus-queue-size 100000 \
  --l2-adapter '{"type":"nixl_store","backend":"POSIX",
                 "backend_params":{"file_path":"/data/nvme/lmcache-l2",
                                   "use_direct_io":"true"},
                 "pool_size":128}'
```

| Flag | Why this value |
|---|---|
| `--metrics-sample-rate 1.0` | **Non-negotiable for characterization.** The default is 0.01. Every lifecycle histogram (L0 block, L1 chunk, real-reuse) is sampled at that rate; on a short run you'd get single-digit sample counts and pure-noise p99s. Counters are unaffected either way |
| `--event-bus-queue-size 100000` | Default 10000 tail-drops under the burst rates we generate. A dropped event silently corrupts histograms |
| `--chunk-size 256` | The production default. Don't use the 16 from the docs quickstart — that's a demo value for making cache traffic visible on toy prompts, and it will wreck any I/O-size analysis |
| `--l2-adapter ... POSIX ... use_direct_io true` | O_DIRECT so the page cache doesn't sit between LMCache and the device and invalidate every storage measurement |
| `--prometheus-port 9095` | Avoids the Prometheus collision |

Backend options for `--l2-adapter` are `POSIX`, `GDS`, `GDS_MT`, `HF3FS`, `OBJ`.
Start with POSIX for baseline; switch to `GDS` or `GDS_MT` when you want the
GPUDirect Storage path. Note from earlier work that GPUDirect RDMA is unsupported on
DGX Spark — verify GDS actually engages on this host before trusting a GDS run.

You can pass `--l2-adapter` more than once to stack tiers (e.g. SSD + NVMe), and each
gets its own `l2_name` / `adapter_index` label in the metrics. Also relevant for the
prioritization work: `--l2-prefetch-max-in-flight` caps concurrent prefetch requests —
raising it increases L2→L1 throughput at the cost of L1 pressure from in-flight data.

Verify before moving on:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:9095/metrics | grep -c '^lmcache_mp'
pgrep -af "lmcache server"
```

If the metric count is 0 but the process is up, you launched with `--disable-metrics`
or the Prometheus port didn't bind — check the server log.

---

## 4. Start the exporters

```bash
# Host DRAM / CPU / disk
docker run -d --name node_exporter --network host --pid host \
  -v /:/host:ro,rslave \
  quay.io/prometheus/node-exporter:latest --path.rootfs=/host

# GPU: HBM occupancy, utilization, power, PCIe throughput
docker run -d --name dcgm_exporter --gpus all --cap-add SYS_ADMIN \
  --network host nvcr.io/nvidia/k8s/dcgm-exporter:latest

curl -s localhost:9100/metrics | head -3
curl -s localhost:9400/metrics | grep -E 'DCGM_FI_DEV_FB_USED|DCGM_FI_DEV_GPU_UTIL' | head
```

DCGM matters specifically because of the HBM tier. `lmcache_mp_l0_block_*` tells you
about KV block *lifecycle* in HBM; `DCGM_FI_DEV_FB_USED` tells you actual framebuffer
occupancy. You need both to tell "LMCache is evicting blocks" apart from "the GPU is
out of memory," and on the DGX under unified memory `nvidia-smi` won't reliably give
you the second one.

---

## 5. Start vLLM with the MP connector

```bash
export MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export PYTHONHASHSEED=0            # deterministic prefix hashing across runs

vllm serve "$MODEL" \
  --port 8001 \
  --disable-hybrid-kv-cache-manager \
  --kv-transfer-config \
  '{"kv_connector":"LMCacheMPConnector",
    "kv_connector_module_path":"lmcache.integration.vllm.lmcache_mp_connector",
    "kv_role":"kv_both",
    "kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost",
                                 "lmcache.mp.port":5555}}'
```

Three things here are easy to get wrong:

- **`--disable-hybrid-kv-cache-manager` is currently required** by the MP connector.
  Without it you get a confusing startup failure.
- **`kv_connector_module_path` is what selects the LMCache-shipped connector.** On
  vLLM ≥ 0.20.0, naming `LMCacheMPConnector` alone still resolves to vLLM's vendored
  copy. The LMCache-shipped one tracks the current server protocol and gets fixes
  first, so always pass the module path.
- **`PYTHONHASHSEED=0`** — without it, prefix hashing varies between runs and your
  hit rates won't be comparable across the three demos.

There is a shortcut (`--kv-offloading-backend lmcache --kv-offloading-size 5`) that
wires the connector up for you against `tcp://localhost:5555`. It's fine for a smoke
test, but use the explicit form for demos so the config is self-documenting in the
results.

Confirm the connection landed:

```bash
curl -s localhost:8001/health
curl -s localhost:8001/metrics | grep -c '^vllm:'
# LMCache side should now show engine activity once you send traffic
curl -s localhost:9095/metrics | grep lmcache_mp_num_chunks_loaded
```

---

## 6. Bring up Prometheus + Grafana

```bash
cd ~/lmcache-obs
docker compose up -d
docker compose ps
```

`docker-compose.yml` runs both on the host network and mounts:

- `prometheus/prometheus.yml` — scrape config for lmcache-mp, vllm, node, dcgm
- `prometheus/rules/lmcache.rules.yml` — recording rules that recreate the health
  scalar and slow-operation counters we lost moving off IP mode
- `grafana/provisioning/` — datasource and dashboard auto-provisioning, so the
  dashboard is there on first login with no manual import
- `grafana/dashboards/lmcache-mp-tiers.json` — the tier dashboard

Verify targets are UP *before* looking at any panel:

```bash
curl -s localhost:9090/api/v1/targets \
  | python3 -c "
import sys,json
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(f\"{t['labels']['job']:<12} {t['health']:<8} {t['scrapeUrl']}\")
    if t['health'] != 'up': print('   ERROR:', t.get('lastError'))
"
```

Grafana: http://\<host\>:3000, admin/admin. The **LMCache MP — Tiers** dashboard should
already be present under the LMCache folder.

---

## 7. Reconcile metric names — do not skip this

The OTel Prometheus exporter appends unit suffixes, and they are not uniform across
metrics. `lmcache_mp.l1_read` surfaces as `lmcache_mp_l1_read_chunks_total`;
`lmcache_mp.l2_store_completed` as `lmcache_mp_l2_store_completed_requests_total`;
throughput histograms as `..._GB_per_second_bucket`. The dashboard in this bundle uses
the names documented upstream, but **suffixes are a known source of silent "No Data"**
and must be confirmed against your actual scrape.

```bash
./scripts/verify_metrics.sh
```

That script does three things: dumps every `lmcache_mp*` series name that actually
exists, runs each dashboard query against Prometheus and reports how many series come
back, and prints a `sed` line for any name that needs correcting. Anything reporting
`0 series` is a dead panel — fix it before you generate load, not after.

---

## 8. Generate cache traffic

Metrics with no traffic behind them are indistinguishable from broken metrics, so
prove the pipeline end-to-end with the two-request shared-prefix test first:

```bash
./scripts/generate_load.sh smoke
```

That sends two completions with a long shared prefix. The first should log
`Stored N tokens`; the second `Retrieved N tokens`. Then:

```bash
curl -sG localhost:9090/api/v1/query --data-urlencode \
  'query=lmcache_mp_lookup_hit_tokens_total' | python3 -m json.tool | head -20
```

Non-zero means the whole chain works. Then run real load:

```bash
./scripts/generate_load.sh bench     # sustained shared-prefix load
```

For the actual demos, drive this with the TensorMesh V3 agentic traces rather than the
synthetic generator — the reuse-gap distributions are the whole point, and synthetic
uniform-prefix load produces a reuse pattern nothing like real agentic traffic. The
generator here is for validating the observability stack, not for producing results.

---

## 9. Validity checks — record these per run

Two things must be true or the run is void. Both are cheap:

```bash
# 1. The EventBus dropped nothing. Non-zero invalidates every histogram.
curl -sG localhost:9090/api/v1/query --data-urlencode \
  'query=lmcache_mp_event_bus_dropped_events_total' \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print('dropped:', r[0]['value'][1] if r else 0)"

# 2. Post-lookup anomaly counter is zero. Non-zero = a lookup/reserve race or
#    unexpected eviction — a correctness problem, not a performance one.
curl -sG localhost:9090/api/v1/query --data-urlencode \
  'query=sum(lmcache_mp_l1_read_failure_total)' \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print('read_failures:', r[0]['value'][1] if r else 0)"

# 3. Confirm the sample rate that was actually in effect
grep -o '\--metrics-sample-rate [0-9.]*' results/lmcache-cmdline.txt
```

`scripts/verify_metrics.sh --post-run` bundles all three and appends to
`results/run-validity.txt`.

---

## 10. Optional: tracing

Per-request tracing is the biggest capability the old IP-mode stack didn't have, and
it's the fastest way to answer "which requests missed and why."

```bash
docker run -d --name tom -p 16686:16686 -p 4317:4317 \
  jaegertracing/all-in-one:latest

# Restart the MP server with tracing on.
# --enable-tracing REQUIRES --otlp-endpoint; the server refuses to start without it.
./scripts/start_lmcache.sh --tracing
```

Jaeger UI at :16686. Look for the `request` root span — it carries `hit_tokens`,
`requested_tokens`, and a precomputed `hit_rate` float. Store-only requests that never
call `lookup_prefetch_start()` won't have these attributes, which is expected, not a bug.

If you'd rather query traces from Grafana, run Tempo instead of Jaeger and use TraceQL:

```
{ name = "request" && span.hit_rate < 0.5 }
{ name = "request" && span.requested_tokens > 0 && span.hit_tokens = 0 }
```

---

## 11. Optional: trace record/replay

Worth a look for the fidelity-methodology work — the `.lct` format carries a schema
version and a SHA-256 digest of the active `StorageManagerConfig`, so replay detects a
mismatched configuration rather than silently producing wrong numbers.

```bash
./scripts/start_lmcache.sh --trace-record /data/nvme/traces/run01.lct
# ... drive load ...
ls -lh /data/nvme/traces/run01.lct
lmcache trace --help
```

Recording runs on the EventBus drain thread, off the request path, and costs a single
boolean check per StorageManager call when disabled. KV tensor bytes are not captured —
replay exercises bookkeeping and controller logic with zero payloads.

---

## 12. Capture screenshots

```bash
docker exec -it grafana grafana cli plugins install grafana-image-renderer
docker restart grafana
sleep 15

# Create a service account token in the Grafana UI, then:
export GRAFANA_TOKEN=<token>
./scripts/50_capture_shots.sh
```

Renders the ten-shot list into `shots/`. Always with `&tz=UTC` and an absolute time
range — relative ranges make screenshots from different demo runs silently
incomparable, which is exactly the failure we're trying to avoid.

---

## 13. Teardown

```bash
docker compose down
docker rm -f node_exporter dcgm_exporter tom 2>/dev/null
pkill -f "lmcache server"
pkill -f "vllm serve"

# L2 data persists on the NVMe — clear it between runs or your second run
# starts warm and the hit-rate comparison is meaningless.
rm -rf /data/nvme/lmcache-l2/*
```

That last step catches people out constantly. A warm L2 from a previous run will make
run 2 look dramatically better than run 1 for reasons that have nothing to do with the
feature under test.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| All Grafana panels "No Data", targets UP | Metric name suffix mismatch | `./scripts/verify_metrics.sh` |
| Prometheus won't start | Port 9090 taken by LMCache | LMCache must use `--prometheus-port 9095` |
| `lmcache_mp*` count is 0 but process alive | `--disable-metrics`, or port didn't bind | Check server log, check `ss -ltnp \| grep 9095` |
| vLLM starts but no LMCache traffic | Hybrid KV cache manager | Add `--disable-hybrid-kv-cache-manager` |
| vLLM connector loads but behaves oddly | Using vLLM's vendored connector | Pass `kv_connector_module_path` |
| `Cannot re-initialize CUDA in forked subprocess` | Fork start method | `export VLLM_WORKER_MULTIPROC_METHOD=spawn` |
| Histograms have almost no samples | Default 1% sampling | `--metrics-sample-rate 1.0` |
| `event_bus_dropped_events_total` climbing | Queue tail-dropping | Raise `--event-bus-queue-size` |
| L2 throughput implausibly high | Page cache, not the device | `"use_direct_io":"true"` in the adapter |
| Eviction count is 0 on a short run | 1 Hz eviction loop outran the benchmark | Check `l1_eviction_loop_ticks_total` vs `_triggered_total`; lengthen the run |
| Hit rate differs between identical runs | Prefix hashing not pinned | `export PYTHONHASHSEED=0` |
| `l2_usage_bytes` missing for one adapter | `get_usage()` raised, skipped silently | Cross-check the store/load counters for that `l2_name` |
| GDS backend slower than POSIX | GDS not actually engaging | Verify GPUDirect Storage support on this host |

---

## File map

```
lmcache-obs/
├── README.md                                  this file
├── docker-compose.yml
├── prometheus/
│   ├── prometheus.yml
│   └── rules/lmcache.rules.yml                health scalar + slow-op recording rules
├── grafana/
│   ├── provisioning/datasources/prometheus.yml
│   ├── provisioning/dashboards/dashboards.yml
│   └── dashboards/lmcache-mp-tiers.json       HBM / DRAM / NVMe tier dashboard
├── scripts/
│   ├── install.sh
│   ├── start_lmcache.sh
│   ├── start_vllm.sh
│   ├── verify_metrics.sh                   run this before trusting any panel
│   ├── generate_load.sh
│   └── capture_shots.sh
├── results/
└── shots/
```
