#!/usr/bin/env bash
# Start vLLM wired to the LMCache MP server.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

MODEL="Qwen/Qwen2.5-7B-Instruct"
# MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
VLLM_PORT="${VLLM_PORT:-8001}"
ZMQ_PORT="${ZMQ_PORT:-5555}"
GPU_UTIL="${GPU_UTIL:-0.85}"

# spawn, not fork -- otherwise "Cannot re-initialize CUDA in forked subprocess"
export VLLM_WORKER_MULTIPROC_METHOD=spawn
# Pin prefix hashing so hit rates are comparable across runs. Without this the
# three demos cannot be compared to each other.
export PYTHONHASHSEED=0

KV_CFG=$(cat <<'JSON'
{"kv_connector":"LMCacheMPConnector",
 "kv_connector_module_path":"lmcache.integration.vllm.lmcache_mp_connector",
 "kv_role":"kv_both",
 "kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost","lmcache.mp.port":ZMQPORT}}
JSON
)
KV_CFG="${KV_CFG//ZMQPORT/$ZMQ_PORT}"
KV_CFG="$(echo "$KV_CFG" | tr -d '\n')"

mkdir -p results
{ echo "MODEL=$MODEL"; echo "PYTHONHASHSEED=$PYTHONHASHSEED"; echo "kv-transfer-config=$KV_CFG"; } \
  > results/vllm-cmdline.txt

# --disable-hybrid-kv-cache-manager is currently REQUIRED by the MP connector.
exec vllm serve "$MODEL" \
  --port "$VLLM_PORT" \
  --download-dir "/data/model" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --disable-hybrid-kv-cache-manager \
  --kv-transfer-config "$KV_CFG" \
  2>&1 | tee results/vllm-server.log
