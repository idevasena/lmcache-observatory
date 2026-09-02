#!/usr/bin/env python3
"""
Generate grafana/dashboards/lmcache-mp-tiers.json.

Written as a generator rather than hand-edited JSON so panel geometry stays
consistent and metric names live in exactly one place -- when 30_verify_metrics.sh
finds a suffix mismatch, fix it in the METRICS dict below and re-run this.

    python3 scripts/gen_dashboard.py
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "PROM_LMCACHE"}

# ---------------------------------------------------------------------------
# Metric names. These follow the upstream docs, but the OTel Prometheus exporter
# appends unit suffixes that are NOT uniform across metrics. Confirm every one of
# these against a live scrape (scripts/30_verify_metrics.sh) before trusting a panel.
# ---------------------------------------------------------------------------
M = {
    # lookup / hit rate
    "lookup_hit":        "lmcache_mp_lookup_hit_tokens_total",
    "lookup_requested":  "lmcache_mp_lookup_requested_tokens_total",
    # L0 (HBM)
    "l0_lifetime":       "lmcache_mp_l0_block_lifetime_seconds",
    "l0_idle":           "lmcache_mp_l0_block_idle_before_evict_seconds",
    "l0_reuse":          "lmcache_mp_l0_block_reuse_gap_seconds",
    "l0l1_store_tp":     "lmcache_mp_l0_l1_store_throughput_GB_per_second",
    "l0l1_load_tp":      "lmcache_mp_l0_l1_load_throughput_GB_per_second",
    # L1 (DRAM)
    "l1_usage_bytes":    "lmcache_mp_l1_memory_usage_bytes",
    "l1_usage_ratio":    "lmcache_mp_l1_usage_ratio",
    "l1_read":           "lmcache_mp_l1_read_chunks_total",
    "l1_write":          "lmcache_mp_l1_write_chunks_total",
    "l1_evicted":        "lmcache_mp_l1_evicted_chunks_total",
    "l1_evict_ticks":    "lmcache_mp_l1_eviction_loop_ticks_total",
    "l1_evict_fired":    "lmcache_mp_l1_eviction_loop_triggered_total",
    "l1_lifetime":       "lmcache_mp_l1_chunk_lifetime_seconds",
    "real_reuse_gap":    "lmcache_mp_real_reuse_gap_seconds",
    "real_reuse_objs":   "lmcache_mp_real_reuse_gap_objects",
    # L2 (NVMe)
    "l2_usage_bytes":    "lmcache_mp_l2_usage_bytes",
    "l2_store_done":     "lmcache_mp_l2_store_completed_requests_total",
    "l2_load_done":      "lmcache_mp_l2_load_completed_requests_total",
    "l2_evicted":        "lmcache_mp_l2_evicted_objects_total",
    "l2_store_tp":       "lmcache_mp_l2_store_throughput_GB_per_second",
    "l2_load_tp":        "lmcache_mp_l2_load_throughput_GB_per_second",
    "inflight_stores":   "lmcache_mp_num_inflight_l2_stores",
    "inflight_loads":    "lmcache_mp_num_inflight_l2_loads",
    "inflight_bytes":    "lmcache_mp_inflight_load_memory_usage_bytes",
    "prefetch_jobs":     "lmcache_mp_active_prefetch_jobs",
    # failures / health
    "f_alloc":           "lmcache_mp_l1_allocation_failure_total",
    "f_read":            "lmcache_mp_l1_read_failure_total",
    "f_prefetch":        "lmcache_mp_l2_prefetch_failure_total",
    "bus_depth":         "lmcache_mp_event_bus_queue_depth",
    "bus_lag":           "lmcache_mp_event_bus_drain_lag_seconds",
    "bus_dropped":       "lmcache_mp_event_bus_dropped_events_total",
    "bus_exc":           "lmcache_mp_event_bus_subscriber_exceptions",
    # engine / external
    "chunks_loaded":     "lmcache_mp_num_chunks_loaded_total",
    "ttft":              "vllm:time_to_first_token_seconds",
    "gpu_fb_used":       "DCGM_FI_DEV_FB_USED",
    "gpu_util":          "DCGM_FI_DEV_GPU_UTIL",
}

_id = [0]
_y = [0]


def nid():
    _id[0] += 1
    return _id[0]


def targets(exprs):
    out = []
    for i, (expr, legend) in enumerate(exprs):
        out.append({
            "datasource": DS, "editorMode": "code", "expr": expr,
            "legendFormat": legend, "range": True, "refId": chr(65 + i),
        })
    return out


def row(title):
    p = {"type": "row", "title": title, "id": nid(), "collapsed": False,
         "panels": [], "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y[0]}}
    _y[0] += 1
    return p


def ts(title, exprs, x, w, h=8, unit="short", desc="", minv=None, maxv=None,
       stack=False, fill=8):
    """timeseries panel"""
    custom = {
        "drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2,
        "fillOpacity": fill, "showPoints": "never", "spanNulls": True,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "axisSoftMin": minv,
    }
    p = {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": _y[0]},
        "targets": targets(exprs),
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": custom,
                         "min": minv, "max": maxv,
                         "color": {"mode": "palette-classic"}},
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom",
                       "showLegend": True, "calcs": ["mean", "max", "lastNotNull"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    return p


def stat(title, exprs, x, w, h=5, unit="short", desc="", steps=None, dec=2):
    p = {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": _y[0]},
        "targets": targets(exprs),
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": dec,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute",
                           "steps": steps or [{"color": "text", "value": None}]},
        }, "overrides": []},
        "options": {"colorMode": "value", "graphMode": "area",
                    "justifyMode": "auto", "orientation": "auto",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "textMode": "auto"},
    }
    return p


def heat(title, metric, x, w, h=9, desc=""):
    """Histogram bucket heatmap -- the right rendering for lifecycle/reuse-gap data."""
    p = {
        "type": "heatmap", "title": title, "description": desc, "id": nid(),
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": _y[0]},
        "targets": [{
            "datasource": DS, "editorMode": "code",
            "expr": f"sum(increase({metric}_bucket[$__rate_interval])) by (le)",
            "format": "heatmap", "legendFormat": "{{le}}", "range": True, "refId": "A",
        }],
        "options": {
            "calculate": False,
            "cellGap": 1,
            "color": {"mode": "scheme", "scheme": "Turbo", "steps": 64,
                      "reverse": False, "exponent": 0.5, "fill": "dark-orange"},
            "yAxis": {"unit": "s", "axisPlacement": "left"},
            "legend": {"show": True},
            "tooltip": {"show": True, "yHistogram": True},
            "rowsFrame": {"layout": "auto"},
        },
        "fieldConfig": {"defaults": {"custom": {"hideFrom":
                        {"tooltip": False, "viz": False, "legend": False}}},
                        "overrides": []},
    }
    return p


def quantiles(metric, by="", q=(0.5, 0.95, 0.99)):
    grp = f"le, {by}" if by else "le"
    lbl = "{{" + by + "}} " if by else ""
    return [
        (
            f"histogram_quantile({v}, "
            f"sum(rate({metric}_bucket[$__rate_interval])) by ({grp}))",
            f"{lbl}p{int(v * 100)}",
        )
        for v in q
    ]


panels = []


def add(*ps):
    panels.extend(ps)
    _y[0] += max(p["gridPos"]["h"] for p in ps)


# =========================================================================
# Row 0 -- Run validity. Deliberately first: if this row is red, nothing
# below it is trustworthy and you should stop reading.
# =========================================================================
panels.append(row("Run validity — check this before reading anything else"))

add(
    stat("Health", [("lmcache:is_healthy", "healthy")], 0, 4, unit="short", dec=0,
         desc="Composite: 0 if the EventBus is dropping, l1_read_failure is moving, "
              "or L1 allocations are failing.",
         steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("EventBus dropped events", [(f"sum({M['bus_dropped']})", "dropped")], 4, 4,
         unit="short", dec=0,
         desc="MUST be 0. Non-zero means the bus tail-dropped at "
              "--event-bus-queue-size and every histogram on this dashboard is wrong.",
         steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Post-lookup anomalies (l1_read_failure)", [(f"sum({M['f_read']})", "failures")],
         8, 5, unit="short", dec=0,
         desc="MUST be 0. In MP mode reserve_read only runs after a successful "
              "lookup, so non-zero means a lookup/reserve race or unexpected "
              "eviction. Correctness problem, not performance.",
         steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("EventBus drain lag", [(M["bus_lag"], "lag")], 13, 4, unit="s",
         desc="Seconds since the oldest queued event. Rising = drain thread "
              "falling behind.",
         steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.5},
                {"color": "red", "value": 2}]),
    stat("Token hit rate", [("lmcache:token_hit_rate", "hit rate")], 17, 7,
         unit="percentunit",
         desc="L1+L2 token-level hit rate. L0 (vLLM's own prefix cache) is "
              "excluded — it is not observable from LMCache.",
         steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0.2},
                {"color": "green", "value": 0.5}]),
)

# =========================================================================
# Row 1 -- Outcome
# =========================================================================
panels.append(row("Outcome — what the user actually sees"))

add(
    ts("TTFT (vLLM)", quantiles(M["ttft"]), 0, 12, unit="s",
       desc="The user-visible payoff. Every storage feature is ultimately "
            "claiming to move this."),
    ts("Token hit rate over time", [
        ("lmcache:token_hit_rate", "aggregate"),
        ("lmcache:token_hit_rate_by_model", "{{model_name}}"),
    ], 12, 12, unit="percentunit", minv=0, maxv=1,
       desc="Should be roughly INVARIANT across storage-feature changes. If it "
            "moves between baseline and treatment, the workload changed and the "
            "comparison is void."),
)

# =========================================================================
# Row 2 -- HBM (L0)
# =========================================================================
panels.append(row("L0 — HBM (GPU) tier"))

add(
    ts("GPU framebuffer used (DCGM)", [(f"{M['gpu_fb_used']}", "GPU {{gpu}}")],
       0, 8, unit="decmbytes",
       desc="Actual HBM occupancy. Needed alongside l0_block_* because on the DGX "
            "under unified memory nvidia-smi does not report this reliably."),
    ts("GPU utilization", [(M["gpu_util"], "GPU {{gpu}}")], 8, 8, unit="percent",
       minv=0, maxv=100,
       desc="Context for whether the GPU was actually the bottleneck."),
    ts("Chunks delivered to engine", [
        (f"rate({M['chunks_loaded']}[$__rate_interval])", "worker {{worker_id}}")],
       16, 8, unit="cps",
       desc="What the MP server hands back to each vLLM worker per retrieve()."),
)

add(
    heat("L0 block lifetime (HBM)", M["l0_lifetime"], 0, 8,
         desc="Allocation to eviction, per sampled GPU block. SAMPLED — set "
              "--metrics-sample-rate 1.0 or this is noise."),
    heat("L0 block idle before evict", M["l0_idle"], 8, 8,
         desc="Last access to eviction. Separates 'never reused' from "
              "'evicted while still hot'."),
    heat("L0 block reuse gap", M["l0_reuse"], 16, 8,
         desc="Gap between consecutive accesses of the same GPU block."),
)

# =========================================================================
# Row 3 -- DRAM (L1)
# =========================================================================
panels.append(row("L1 — DRAM (CPU) tier"))

add(
    ts("L1 occupancy", [(M["l1_usage_bytes"], "bytes held")], 0, 8, unit="bytes",
       desc="Rising without plateau suggests a leak. Saturating at --l1-size-gb "
            "means the working set exceeds capacity."),
    ts("L1 usage ratio vs eviction watermark", [
        (M["l1_usage_ratio"], "usage ratio"),
        ("vector(0.8)", "watermark (default 0.8)"),
    ], 8, 8, unit="percentunit", minv=0, maxv=1,
       desc="A demo run that never approaches the watermark is not exercising the "
            "tier and proves nothing about storage."),
    ts("L1 chunk rates", [
        (f"rate({M['l1_read']}[$__rate_interval])", "read {{cache_salt}}"),
        (f"rate({M['l1_write']}[$__rate_interval])", "write {{cache_salt}}"),
        (f"rate({M['l1_evicted']}[$__rate_interval])", "evicted {{cache_salt}}"),
    ], 16, 8, unit="cps"),
)

add(
    ts("Eviction loop: alive vs fired", [
        (f"rate({M['l1_evict_ticks']}[$__rate_interval])", "loop ticks"),
        (f"rate({M['l1_evict_fired']}[$__rate_interval])", "eviction fired"),
    ], 0, 8, unit="cps",
       desc="The loop polls at ~1 Hz. On a short benchmark it can tick without ever "
            "firing — that is why upstream exports both. Ticks with no fires means "
            "the run ended before the watermark was crossed."),
    heat("L1 chunk lifetime", M["l1_lifetime"], 8, 8,
         desc="THE FDP METRIC. If this distribution is multimodal with N modes, "
              "that is the empirical case for N RUHs. If unimodal, more RUHs buy "
              "nothing and we should say so."),
    heat("Real reuse gap (time)", M["real_reuse_gap"], 16, 8,
         desc="Time between a chunk's last access and its next read — storage cost. "
              "Short gaps mean prefetch has little slack, which is what makes read "
              "prioritization matter."),
)

# =========================================================================
# Row 4 -- NVMe (L2)
# =========================================================================
panels.append(row("L2 — NVMe tier"))

add(
    ts("L2 occupancy by backend", [(M["l2_usage_bytes"], "{{l2_name}}")], 0, 8,
       unit="bytes",
       desc="A missing series for an l2_name means either 'not configured' or "
            "'get_usage() raised this scrape' — cross-check the store/load counters."),
    ts("L2 IOPS (fixed 1m window)", [
        ("lmcache:l2_store_iops", "store {{l2_name}}"),
        ("lmcache:l2_load_iops", "load {{l2_name}}"),
    ], 8, 8, unit="iops",
       desc="Upstream exports no *_iops metric so dashboards pick their own window. "
            "Pinned to 1m in the recording rules so all three demos are comparable."),
    ts("L2 evictions", [
        (f"rate({M['l2_evicted']}[$__rate_interval])", "{{cache_salt}}")],
       16, 8, unit="cps",
       desc="Invalidation rate — the trim/deallocate traffic the device sees. "
            "Relevant to FDP reclaim behaviour."),
)

add(
    ts("L1→L2 store throughput (submit→complete)", quantiles(M["l2_store_tp"], "l2_name"),
       0, 12, unit="GBs",
       desc="PRIMARY for FDP and MDTS. Includes adapter queue, network and disk — "
            "this is bytes / end-to-end latency, not raw transfer rate. That is the "
            "right measure for MDTS (fewer commands is the benefit) but it means you "
            "cannot separate 'fewer commands' from 'faster device' here."),
    ts("L2→L1 load throughput (submit→complete)", quantiles(M["l2_load_tp"], "l2_name"),
       12, 12, unit="GBs",
       desc="PRIMARY for read prioritization. Watch p99 against the store p99 — "
            "that gap is the read-under-write-pressure story."),
)

add(
    ts("In-flight L2 stores vs loads", [
        (M["inflight_stores"], "store {{l2_name}}/{{adapter_index}}"),
        (M["inflight_loads"], "load {{l2_name}}/{{adapter_index}}"),
    ], 0, 8,
       desc="THE CONTENTION SIGNAL. Sustained non-zero stores means the adapter "
            "cannot keep up with the L1→L2 write rate. The loads/stores balance is "
            "exactly what read prioritization is meant to shift."),
    ts("Active prefetch jobs", [(M["prefetch_jobs"], "in flight")], 8, 8,
       desc="Sustained high values indicate a slow L2 backend or polling delay. "
            "Should FALL with effective read prioritization."),
    ts("In-flight prefetch L1 reservation", [
        (M["inflight_bytes"], "{{l2_name}}/{{adapter_index}}")], 16, 8, unit="bytes",
       desc="L1 bytes reserved by in-flight prefetch loads. Rising alongside L1 "
            "occupancy means prefetch reservations are crowding out cacheable data — "
            "a plausible way for prioritization to help one metric and hurt another."),
)

# =========================================================================
# Row 5 -- Transfers between tiers (control variables)
# =========================================================================
panels.append(row("Tier transfers — HBM↔DRAM (MDTS control variable)"))

add(
    ts("L0→L1 store throughput (GPU→CPU)", quantiles(M["l0l1_store_tp"]), 0, 12,
       unit="GBs",
       desc="CONTROL VARIABLE for the MDTS sweep. True GPU-stream copy time, "
            "unaffected by MDTS. If this moves between MDTS configurations, "
            "something other than MDTS changed and the comparison is invalid."),
    ts("L1→L0 load throughput (CPU→GPU)", quantiles(M["l0l1_load_tp"]), 12, 12,
       unit="GBs", desc="Same, load direction."),
)

# =========================================================================
# Row 6 -- Failures and telemetry health
# =========================================================================
panels.append(row("Failures & telemetry health"))

add(
    ts("Failure counters", [
        (f"rate({M['f_alloc']}[$__rate_interval])", "L1 alloc fail {{during}}/{{model_name}}"),
        (f"rate({M['f_read']}[$__rate_interval])", "L1 read fail {{reason}}/{{during}}"),
        (f"rate({M['f_prefetch']}[$__rate_interval])", "L2 prefetch fail {{reason}}"),
    ], 0, 12, unit="cps",
       desc="l1_allocation_failure = L1 OOM during reserve_write. "
            "l2_prefetch_failure{reason=l1_oom} = prefetch arrived but found no room — "
            "directly relevant to whether read prioritization is landing in time."),
    ts("EventBus health", [
        (M["bus_depth"], "queue depth"),
        (M["bus_lag"], "drain lag (s)"),
        (f"rate({M['bus_dropped']}[$__rate_interval])", "dropped/s"),
        (f"rate({M['bus_exc']}[$__rate_interval])", "subscriber exc {{subscriber_name}}"),
    ], 12, 12,
       desc="These observe bus state directly rather than being event-driven, so a "
            "failing subscriber cannot silence them. Proves the MEASUREMENT was "
            "clean, not just the workload."),
)

dashboard = {
    "uid": "lmcache-mp-tiers",
    "title": "LMCache MP — Tiers (HBM / DRAM / NVMe)",
    "description": (
        "Tier-aware LMCache MP observability. Replaces the legacy IP-mode "
        "HBM/DRAM dashboard, which targeted the lmcache:* namespace and renders "
        "empty under MP mode. Row 0 is run validity — if it is red, stop."
    ),
    "tags": ["lmcache", "kv-cache", "storage", "mp-mode"],
    "timezone": "utc",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "5s",
    "time": {"from": "now-30m", "to": "now"},
    "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
    "templating": {"list": [
        {"name": "l2_name", "label": "L2 backend", "type": "query",
         "datasource": DS, "refresh": 2, "includeAll": True, "multi": True,
         "query": {"query": f"label_values({M['l2_usage_bytes']}, l2_name)",
                   "refId": "A"},
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}},
        {"name": "model_name", "label": "Model", "type": "query",
         "datasource": DS, "refresh": 2, "includeAll": True, "multi": True,
         "query": {"query": f"label_values({M['lookup_requested']}, model_name)",
                   "refId": "A"},
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}},
    ]},
    "annotations": {"list": [{
        "name": "Alerts", "datasource": DS, "enable": True,
        "iconColor": "red", "type": "dashboard",
    }]},
    "panels": panels,
}

out = pathlib.Path(__file__).resolve().parent.parent / "grafana/dashboards/lmcache-mp-tiers.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out}  ({len(panels)} panels, {out.stat().st_size} bytes)")

# Emit the flat metric list so 30_verify_metrics.sh can check every one.
names = sorted(set(M.values()))
ml = out.parent.parent.parent / "scripts/dashboard_metrics.txt"
ml.write_text("\n".join(names) + "\n")
print(f"wrote {ml}  ({len(names)} distinct metric names)")
