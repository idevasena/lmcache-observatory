#!/usr/bin/env python3
"""Drive LMCache MP hard enough to populate every dashboard panel.

The old smoke test sent two requests. That exercises lookup and L1 store and
nothing else: without eviction there are no L1->L2 stores, without re-reading
evicted content there is no prefetch or L2->L1 load, and without concurrency the
in-flight gauges never leave zero. Roughly half the dashboard stays blank.

This runs six phases, each targeting metric families the previous one cannot
reach:

  1 COLD     unique long prefixes, first touch     -> l1_write, lookup_requested
  2 HOT      immediate re-read of a subset         -> lookup_hit, short reuse gaps
  3 FLOOD    unique traffic past L1 capacity       -> l1_evicted, eviction_loop_
                                                      triggered, l2_store_*
  4 REVISIT  re-read prefixes evicted in phase 3   -> l2_prefetch_*, l2_load_*,
                                                      l1_chunk_evict_reuse_gap
  5 MIX      concurrent read/write contention      -> num_inflight_l2_{loads,
                                                      stores}, active_prefetch_jobs,
                                                      inflight_load_memory_usage
  6 SOAK     Zipfian reuse, sustained              -> real_reuse_gap{,_objects},
                                                      stable rate windows

Phase 3 is sized from the model's actual KV geometry against --l1-size-gb, so
eviction is guaranteed rather than hoped for.

Finishes with a coverage report naming any dashboard metric still absent.

    python3 scripts/40_generate_load.py --l1-size-gb 100
    python3 scripts/40_generate_load.py --quick        # pipeline check only
"""
import argparse
import json
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Deterministic vocabulary -- reproducible prefixes across runs, which matters
# because hit rates are only comparable if the workload is identical.
VOCAB = """storage cache latency throughput device kernel buffer stream handle
placement reclaim unit tensor attention prefix token block eviction watermark
adapter prefetch retrieve serialize offload tier occupancy bandwidth queue depth
submission completion controller allocator fragment checkpoint pipeline scheduler
inference decode prefill embedding transformer residual normalize quantize
compression dictionary window sequence batch concurrency saturation percentile
histogram counter gauge instrument telemetry observability dashboard panel""".split()

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- HTTP helpers
def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_text(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def count_tokens(vllm, model, text):
    """vLLM exposes /tokenize -- use it instead of guessing at token counts."""
    try:
        return post(f"{vllm}/tokenize", {"model": model, "prompt": text})["count"]
    except Exception:
        return None


# ------------------------------------------------------------- model geometry
def kv_bytes_per_token(vllm, model):
    """KV bytes/token from the served model config, for capacity sizing.

    Falls back to a Llama-3.1-8B-shaped guess if transformers is unavailable;
    the phase sizing only needs the right order of magnitude.
    """
    try:
        from transformers import AutoConfig
        c = AutoConfig.from_pretrained(model)
        kv = getattr(c, "num_key_value_heads", None) or c.num_attention_heads
        hd = getattr(c, "head_dim", None) or (c.hidden_size // c.num_attention_heads)
        b = 2 * c.num_hidden_layers * kv * hd * 2          # K+V, fp16
        log(f"    KV geometry: {c.num_hidden_layers} layers x {kv} kv_heads x "
            f"{hd} head_dim -> {b/1024:.1f} KiB/token")
        return b
    except Exception as e:
        log(f"    could not read model config ({type(e).__name__}); "
            f"assuming 128 KiB/token")
        return 128 * 1024


# ------------------------------------------------------------ prompt building
class PromptFactory:
    """Builds chunk-aligned prefixes of a target token length."""

    def __init__(self, vllm, model, target_tokens, seed=1337):
        self.rng = random.Random(seed)
        self.vllm = vllm
        self.model = model
        self.target = target_tokens
        self.words_per_token = 0.75          # calibrated below
        self._calibrate()

    def _words(self, n):
        return " ".join(self.rng.choice(VOCAB) for _ in range(n))

    def _calibrate(self):
        probe_words = 400
        text = self._words(probe_words)
        n = count_tokens(self.vllm, self.model, text)
        if n:
            self.words_per_token = probe_words / n
            log(f"    tokenizer calibration: {n} tokens from {probe_words} words "
                f"({self.words_per_token:.2f} words/token)")
        else:
            log("    /tokenize unavailable; using 0.75 words/token estimate")

    def make(self, tag):
        """A unique prefix of ~target tokens. `tag` makes it distinct."""
        nwords = int(self.target * self.words_per_token)
        return f"[doc:{tag}] " + self._words(nwords)


# --------------------------------------------------------------- request loop
class Driver:
    def __init__(self, vllm, model, max_tokens=8, cache_salts=None):
        self.vllm = vllm
        self.model = model
        self.max_tokens = max_tokens
        self.salts = cache_salts or []
        self.ok = 0
        self.fail = 0
        self.latencies = []
        self.errors = Counter()
        self.lock = threading.Lock()

    def send(self, prefix, suffix="", salt=None):
        payload = {
            "model": self.model,
            "prompt": f"{prefix}\n\nQuestion: {suffix}\nAnswer:",
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        # Per-tenant isolation. Populates the cache_salt label that most L1/L2
        # counters carry. Harmless if the server ignores it.
        if salt:
            payload["cache_salt"] = salt
        t0 = time.time()
        try:
            post(f"{self.vllm}/v1/completions", payload)
            dt = time.time() - t0
            with self.lock:
                self.ok += 1
                self.latencies.append(dt)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            with self.lock:
                self.fail += 1
                self.errors[f"HTTP {e.code}: {body[:80]}"] += 1
            # cache_salt unsupported on this build -- retry without it once
            if salt and e.code == 400 and "cache_salt" in body:
                self.salts = []
                return self.send(prefix, suffix, salt=None)
            return False
        except Exception as e:
            with self.lock:
                self.fail += 1
                self.errors[type(e).__name__] += 1
            return False

    def run(self, jobs, concurrency, label):
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(self.send, p, s, salt) for p, s, salt in jobs]
            for f in as_completed(futs):
                done += 1
                if done % max(1, len(futs) // 10) == 0:
                    log(f"      {label}: {done}/{len(futs)} "
                        f"({time.time()-t0:.0f}s)")
        return time.time() - t0


# ------------------------------------------------------------ metrics scraping
def scrape(url):
    """Return {metric_name: total_value} from a Prometheus exposition page."""
    try:
        text = get_text(url)
    except Exception as e:
        log(f"    cannot scrape {url}: {e}")
        return {}
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{")[0].split(" ")[0]
        try:
            val = float(line.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def coverage_report(before, after):
    """Which dashboard metrics exist, and which actually moved."""
    wanted = []
    mf = ROOT / "scripts/dashboard_metrics.txt"
    if mf.exists():
        wanted = [l.strip() for l in mf.read_text().splitlines() if l.strip()]
    wanted = [m for m in wanted if m.startswith("lmcache_mp")]

    # These SHOULD stay at zero on a healthy run. Flat is the pass condition,
    # not a gap -- reporting them as "flat, panel will be a straight line"
    # would invite someone to go looking for a problem that isn't there.
    EXPECT_ZERO = ("_failure", "dropped_events", "subscriber_exceptions")

    def strip_suffix(name, suffix):
        return name[:-len(suffix)] if name.endswith(suffix) else name

    present, moved, absent, flat, clean = [], [], [], [], []
    for m in wanted:
        # histograms surface as _bucket/_sum/_count
        keys = [k for k in after
                if k == m or k.startswith(m + "_") or strip_suffix(k, "_total") == m]
        if not keys:
            absent.append(m)
            continue
        present.append(m)
        b = sum(before.get(k, 0.0) for k in keys)
        a = sum(after.get(k, 0.0) for k in keys)
        if any(tok in m for tok in EXPECT_ZERO):
            (moved if a > b else clean).append(m)
        else:
            (moved if a > b else flat).append(m)

    log("\n" + "=" * 70)
    log(" METRIC COVERAGE")
    log("=" * 70)
    log(f"  exposed and moved during this run : {len(moved)}")
    log(f"  exposed but flat                  : {len(flat)}")
    log(f"  health counters correctly at zero : {len(clean)}")
    log(f"  not exposed at all                : {len(absent)}")

    if flat:
        log("\n  FLAT (exposed, no change) -- panel will render a straight line:")
        for m in sorted(flat):
            log(f"    {m}")
    if absent:
        log("\n  ABSENT -- panel will render 'No Data':")
        for m in sorted(absent):
            log(f"    {m}")
        log("\n  Absent usually means one of:")
        log("    - the instrument is lazy and its code path never ran")
        log("    - the metric name suffix differs (run 30_verify_metrics.sh)")
        log("    - the feature is unconfigured (e.g. no L2 adapter -> no l2_*)")

    # A failure counter that MOVED is the real problem.
    for m in moved:
        if any(tok in m for tok in EXPECT_ZERO):
            log(f"\n  *** WARNING: {m} increased. Run is not clean -- see the "
                f"validity row on the dashboard. ***")
    return {"moved": moved, "flat": flat, "clean_zero": clean, "absent": absent}


# ------------------------------------------------------------------- workload
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vllm", default="http://localhost:8001")
    ap.add_argument("--model", default=None,
                    help="defaults to whatever /v1/models reports")
    ap.add_argument("--metrics", default="http://localhost:8080/metrics",
                    help="LMCache MP metrics endpoint (HTTP frontend)")
    ap.add_argument("--l1-size-gb", type=float, default=100.0,
                    help="must match the running server; drives flood sizing")
    ap.add_argument("--chunk-size", type=int, default=256)
    ap.add_argument("--prefix-tokens", type=int, default=2048,
                    help="tokens per document; keep a multiple of chunk-size")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--overflow", type=float, default=1.6,
                    help="multiple of L1 capacity to write in the flood phase")
    ap.add_argument("--soak-seconds", type=int, default=180)
    ap.add_argument("--quick", action="store_true",
                    help="tiny run: validates the pipeline, will NOT fill panels")
    args = ap.parse_args()

    # ---- discover the served model ----
    if not args.model:
        try:
            args.model = json.loads(get_text(f"{args.vllm}/v1/models"))["data"][0]["id"]
        except Exception as e:
            log(f"cannot reach {args.vllm}/v1/models: {e}")
            sys.exit(1)
    log(f"\nmodel     : {args.model}")
    log(f"vllm      : {args.vllm}")
    log(f"metrics   : {args.metrics}")

    # ---- size the flood phase against real KV geometry ----
    bpt = kv_bytes_per_token(args.vllm, args.model)
    l1_bytes = args.l1_size_gb * 1024**3
    flood_tokens = int(l1_bytes * args.overflow / bpt)
    n_flood = max(4, flood_tokens // args.prefix_tokens)

    if args.quick:
        n_cold, n_flood, args.soak_seconds = 4, 6, 20
        log("\n*** --quick: pipeline check only. Panels will NOT be fully "
            "populated. ***")
    else:
        n_cold = max(8, n_flood // 6)

    log(f"\nsizing:")
    log(f"    L1 capacity        {args.l1_size_gb:.0f} GB")
    log(f"    prefix length      {args.prefix_tokens} tokens "
        f"({args.prefix_tokens/args.chunk_size:.0f} chunks of {args.chunk_size})")
    log(f"    flood documents    {n_flood}  "
        f"(~{n_flood*args.prefix_tokens*bpt/1024**3:.1f} GB of KV, "
        f"{args.overflow}x L1)")
    log(f"    cold documents     {n_cold}")
    log(f"    concurrency        {args.concurrency}")

    before = scrape(args.metrics)
    log(f"\n    baseline: {len(before)} series exposed")

    factory = PromptFactory(args.vllm, args.model, args.prefix_tokens)
    drv = Driver(args.vllm, args.model,
                 cache_salts=["tenant-a", "tenant-b", "tenant-c"])
    salts = drv.salts
    t_start = time.time()

    # ---- Phase 1: COLD ----
    log("\n[1/6] COLD  -- unique prefixes, first touch")
    cold = [factory.make(f"cold-{i}") for i in range(n_cold)]
    jobs = [(p, f"summarize {i}", salts[i % len(salts)] if salts else None)
            for i, p in enumerate(cold)]
    dt = drv.run(jobs, args.concurrency, "cold")
    log(f"      done in {dt:.0f}s")

    # ---- Phase 2: HOT ----
    log("\n[2/6] HOT   -- immediate re-read (L1 hits, short reuse gaps)")
    jobs = []
    for rep in range(3):
        for i, p in enumerate(cold):
            jobs.append((p, f"followup {rep}-{i}",
                         salts[i % len(salts)] if salts else None))
    dt = drv.run(jobs, args.concurrency, "hot")
    log(f"      done in {dt:.0f}s")

    # ---- Phase 3: FLOOD ----
    log(f"\n[3/6] FLOOD -- {n_flood} unique docs to force L1 eviction into L2")
    log("      (this is the long phase; it is what makes l2_* and eviction "
        "metrics non-zero)")
    flood = [factory.make(f"flood-{i}") for i in range(n_flood)]
    jobs = [(p, f"scan {i}", salts[i % len(salts)] if salts else None)
            for i, p in enumerate(flood)]
    dt = drv.run(jobs, args.concurrency, "flood")
    log(f"      done in {dt:.0f}s")

    # give the ~1 Hz eviction loop and async L2 stores time to drain
    log("      settling 20s for eviction loop + async L2 stores...")
    time.sleep(20)

    # ---- Phase 4: REVISIT ----
    log("\n[4/6] REVISIT -- re-read phase-1 docs now evicted (L2 prefetch+load)")
    jobs = [(p, f"revisit {i}", salts[i % len(salts)] if salts else None)
            for i, p in enumerate(cold)]
    jobs += [(flood[i], f"revisit-f {i}", salts[i % len(salts)] if salts else None)
             for i in range(0, min(len(flood), 40))]
    dt = drv.run(jobs, args.concurrency, "revisit")
    log(f"      done in {dt:.0f}s")

    # ---- Phase 5: MIX ----
    log("\n[5/6] MIX   -- concurrent new-writes + revisits (in-flight gauges)")
    mix = []
    for i in range(120):
        if i % 2:
            mix.append((factory.make(f"mix-{i}"), f"new {i}",
                        salts[i % len(salts)] if salts else None))
        else:
            src = cold[i % len(cold)]
            mix.append((src, f"reread {i}", salts[i % len(salts)] if salts else None))
    dt = drv.run(mix, args.concurrency * 3, "mix")
    log(f"      done in {dt:.0f}s at concurrency {args.concurrency*3}")

    # ---- Phase 6: SOAK ----
    log(f"\n[6/6] SOAK  -- {args.soak_seconds}s Zipfian reuse "
        f"(populates rate() windows and reuse-gap histograms)")
    pool = cold + flood[:60]
    rng = random.Random(99)
    # Zipf-ish: a few documents get most of the traffic, like real agentic
    # workloads. Uniform reuse produces a reuse-gap distribution that looks
    # nothing like production and would mislead the RUH analysis.
    weights = [1.0 / (i + 1) for i in range(len(pool))]
    end = time.time() + args.soak_seconds
    soak_jobs = []
    while len(soak_jobs) < args.soak_seconds * 4:
        p = rng.choices(pool, weights=weights, k=1)[0]
        soak_jobs.append((p, f"soak {len(soak_jobs)}",
                          salts[len(soak_jobs) % len(salts)] if salts else None))
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = []
        for j in soak_jobs:
            if time.time() > end:
                break
            futs.append(ex.submit(drv.send, *j))
            time.sleep(0.05)
        for _ in as_completed(futs):
            pass
    log(f"      soak complete")

    log("      settling 15s for async L2 + metric scrape alignment...")
    time.sleep(15)

    # ---- results ----
    total = time.time() - t_start
    after = scrape(args.metrics)
    log("\n" + "=" * 70)
    log(" WORKLOAD SUMMARY")
    log("=" * 70)
    log(f"  wall time        {total/60:.1f} min")
    log(f"  requests ok      {drv.ok}")
    log(f"  requests failed  {drv.fail}")
    if drv.errors:
        for e, n in drv.errors.most_common(5):
            log(f"      {n:>5}x {e}")
    if drv.latencies:
        lat = sorted(drv.latencies)
        log(f"  latency p50/p99  {statistics.median(lat):.2f}s / "
            f"{lat[int(len(lat)*0.99)]:.2f}s")
    log(f"  series exposed   {len(before)} -> {len(after)}")

    cov = coverage_report(before, after)

    outdir = ROOT / "results"
    outdir.mkdir(exist_ok=True)
    (outdir / "workload-summary.json").write_text(json.dumps({
        "model": args.model,
        "kv_bytes_per_token": bpt,
        "l1_size_gb": args.l1_size_gb,
        "prefix_tokens": args.prefix_tokens,
        "flood_docs": n_flood,
        "concurrency": args.concurrency,
        "requests_ok": drv.ok,
        "requests_failed": drv.fail,
        "wall_seconds": round(total, 1),
        "series_before": len(before),
        "series_after": len(after),
        "coverage": cov,
    }, indent=2))
    log(f"\n  wrote results/workload-summary.json")

    if drv.fail > drv.ok * 0.05:
        log("\n  *** >5% of requests failed. Treat these metrics as suspect. ***")
        sys.exit(2)


if __name__ == "__main__":
    main()
