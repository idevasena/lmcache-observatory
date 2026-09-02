#!/usr/bin/env bash
# Install LMCache + vLLM into a pinned venv and record the environment.
set -euo pipefail
cd "$(dirname "$0")/.."

LMCACHE_VERSION="${LMCACHE_VERSION:-0.5.4}"
NVME_DEV="${NVME_DEV:-/dev/nvme0}"
NVME_BLK="${NVME_BLK:-nvme0n1}"
L2_PATH="${L2_PATH:-/data/nvme/lmcache-l2}"

command -v uv >/dev/null || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

[[ -d .venv ]] || uv venv --python 3.12
# shellcheck disable=SC1091
source .venv/bin/activate

# NIXL is an optional extra as of 0.5.2 and is required for the POSIX/GDS L2
# adapters -- which is the entire point for storage characterization.
uv pip install "lmcache[nixl]==${LMCACHE_VERSION}"
uv pip install vllm

mkdir -p results "$L2_PATH" 2>/dev/null || sudo mkdir -p "$L2_PATH"
sudo chown -R "$USER:$USER" "$L2_PATH" 2>/dev/null || true

{
  echo "=== captured $(date -u +%FT%TZ) ==="
  echo "host: $(hostname)"
  echo "kernel: $(uname -r)"
  echo
  echo "--- versions ---"
  python -c "import lmcache;print('lmcache', lmcache.__version__)"
  python -c "import vllm;print('vllm', vllm.__version__)" 2>/dev/null || echo "vllm: n/a"
  python -c "import sys;print('python', sys.version.split()[0])"
  echo
  echo "--- gpu ---"
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv
  echo
  echo "--- nvme under test ---"
  nvme id-ctrl "$NVME_DEV" 2>/dev/null | grep -iE '^(mn|sn|mdts)' || echo "nvme-cli unavailable"
  echo "max_hw_sectors_kb: $(cat "/sys/block/$NVME_BLK/queue/max_hw_sectors_kb" 2>/dev/null)"
  echo "max_sectors_kb:    $(cat "/sys/block/$NVME_BLK/queue/max_sectors_kb" 2>/dev/null)"
  echo "scheduler:         $(cat "/sys/block/$NVME_BLK/queue/scheduler" 2>/dev/null)"
  echo "nr_requests:       $(cat "/sys/block/$NVME_BLK/queue/nr_requests" 2>/dev/null)"
  echo
  echo "--- L2 landing directory ---"
  echo "path:   $L2_PATH"
  echo "device: $(findmnt -no SOURCE --target "$L2_PATH" 2>/dev/null)"
  df -h "$L2_PATH" | tail -1
} > results/environment.txt

cat results/environment.txt
echo
echo "Environment recorded in results/environment.txt"
echo "CHECK: is the L2 device above actually the NVMe under test, not the OS drive?"
