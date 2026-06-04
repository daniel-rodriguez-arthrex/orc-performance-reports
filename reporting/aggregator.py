"""
aggregator.py — Merge all collected performance metrics into a single structured dict.
"""

import csv
import statistics
from datetime import datetime


def _p95(lst: list) -> float:
    if not lst:
        return 0.0
    sorted_lst = sorted(lst)
    # ceil(n * 0.95) - 1 gives the correct 0-based index for the 95th percentile.
    # e.g. n=20 → idx=18 (19th value); n=100 → idx=94 (95th value).
    import math
    idx = math.ceil(len(sorted_lst) * 0.95) - 1
    return float(sorted_lst[max(0, idx)])


def _avg(lst: list) -> float:
    return float(statistics.mean(lst)) if lst else 0.0


def aggregate(
    scenario_name: str,
    session_results: list,
    webrtc_snapshots: list,
    api_calls: list,
    server_metrics_csv_path: str = None,
    stream_timing: dict = None,
    process_spikes_csv_path: str = None,
    proc_series_csv_path: str = None,
    ffmpeg_instances_csv_path: str = None,
) -> dict:
    """
    Merge all collected metrics into a single structured dict suitable for
    reporting and serialization.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # --- Sessions ---
    sessions = []
    load_times = []
    connected_count = 0

    for s in session_results:
        connected = bool(s.connected)
        load_ms = float(s.load_time_ms) if s.load_time_ms is not None else 0.0
        sessions.append({
            "tab": int(s.tab_index),
            "connected": connected,
            "modal_fired": bool(s.modal_fired),
            "load_ms": load_ms,
            "layout": str(s.layout) if s.layout else "",
            "username": str(s.username) if s.username else "",
        })
        if connected:
            connected_count += 1
        if load_ms > 0:
            load_times.append(load_ms)

    total_sessions = len(sessions)
    blocked_count = total_sessions - connected_count
    connect_rate = round((connected_count / total_sessions * 100), 2) if total_sessions > 0 else 0.0

    summary = {
        "total_sessions": total_sessions,
        "connected": connected_count,
        "blocked": blocked_count,
        "connect_rate_pct": connect_rate,
        "avg_load_ms": round(_avg(load_times), 2),
        "max_load_ms": round(max(load_times), 2) if load_times else 0.0,
    }

    # --- WebRTC ---
    snapshots = []
    fps_vals = []
    jitter_vals = []
    total_dropped = 0

    # Per-source throughput: use bytes_rx delta over the stable streaming window.
    # bytes_rx_at_start is captured the moment all tabs go live; streaming_duration_s
    # is soak_s minus that ramp-up time, giving the clean steady-state window.
    _bx_start   = (stream_timing or {}).get("bytes_rx_at_start", {})
    _stream_dur = float((stream_timing or {}).get("streaming_duration_s", 0.0))

    for w in webrtc_snapshots:
        fps = float(w.fps) if w.fps is not None else 0.0
        jitter = float(w.jitter_ms) if w.jitter_ms is not None else 0.0
        dropped = int(w.dropped_frames) if w.dropped_frames is not None else 0
        bytes_rx = int(w.bytes_received) if w.bytes_received is not None else 0
        tab_idx  = int(w.tab_index)

        # Compute avg throughput for this source over the stable window
        avg_mbps = None
        if _stream_dur > 0:
            delta = bytes_rx - _bx_start.get(tab_idx, 0)
            if delta > 0:
                avg_mbps = round(delta * 8 / _stream_dur / 1_000_000, 2)

        snapshots.append({
            "time": float(w.timestamp),
            "tab": tab_idx,
            "conns": int(w.connection_count) if w.connection_count is not None else 0,
            "fps": fps,
            "dropped": dropped,
            "bytes_rx": bytes_rx,
            "avg_mbps": avg_mbps,
            "jitter_ms": jitter,
            "rtt_ms": float(w.rtt_ms) if w.rtt_ms is not None else 0.0,
        })
        if fps > 0:
            fps_vals.append(fps)
        if jitter > 0:
            jitter_vals.append(jitter)
        total_dropped += dropped

    webrtc = {
        "snapshots": snapshots,
        "avg_fps": round(_avg(fps_vals), 2),
        "avg_jitter_ms": round(_avg(jitter_vals), 2),
        "total_dropped_frames": total_dropped,
    }

    # --- Per-source bandwidth breakdown ---
    # Uses per-PC bytes delta over the stable streaming window.
    # PC index i corresponds to room_sources[i] (creation order matches display order).
    _per_src_start = (stream_timing or {}).get("per_source_bx_at_start", {})
    per_source_deltas: dict = {}  # src_idx -> list[delta_bytes across connected tabs]

    for w in webrtc_snapshots:
        tab_idx   = int(w.tab_index)
        src_start = _per_src_start.get(tab_idx, [])
        src_end   = list(w.per_source_bytes or []) if w.per_source_bytes is not None else []
        if _stream_dur > 0 and src_end and src_start:
            for src_idx, (end_b, start_b) in enumerate(zip(src_end, src_start)):
                delta = int(end_b) - int(start_b)
                if delta > 0:
                    per_source_deltas.setdefault(src_idx, []).append(delta)

    source_breakdown = []
    for src_idx, deltas in sorted(per_source_deltas.items()):
        avg_delta = sum(deltas) / len(deltas)
        mbps = round(avg_delta * 8 / _stream_dur / 1_000_000, 3)
        source_breakdown.append({
            "source_idx":       src_idx,
            "avg_mbps_per_viewer": mbps,
            "sample_count":     len(deltas),
        })

    # --- API Calls ---
    calls = []
    all_durations = []
    bw_durations = []

    for a in api_calls:
        duration = float(a.duration_ms) if a.duration_ms is not None else 0.0
        is_bw = bool(a.is_bandwidth_check)
        calls.append({
            "time": float(a.timestamp),
            "url": str(a.url),
            "method": str(a.method),
            "duration_ms": duration,
            "status": int(a.status) if a.status is not None else 0,
            "is_bw_check": is_bw,
        })
        all_durations.append(duration)
        if is_bw:
            bw_durations.append(duration)

    # Non-bandwidth ORC API calls (GraphQL, token, etc.) — clean latency bucket.
    # avg_ms is computed from this bucket only so it reflects actual server-side
    # API responsiveness, not bandwidth-check timing which skews the distribution.
    non_bw_durations = [d for d, c in zip(all_durations, api_calls) if not c.is_bandwidth_check]

    api = {
        "calls":         calls,
        "total":         len(calls),
        "non_bw_total":  len(non_bw_durations),
        "bw_checks":     len(bw_durations),
        "avg_ms":        round(_avg(non_bw_durations), 2),   # non-BW only
        "bw_avg_ms":     round(_avg(bw_durations), 2),
        "bw_p95_ms":     round(_p95(bw_durations), 2),
    }

    # --- Server Metrics ---
    server = None
    if server_metrics_csv_path:
        rows = []
        cpu_vals = []
        ram_vals = []
        net_recv_vals = []
        net_send_vals = []

        with open(server_metrics_csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cpu = float(row.get("cpu_percent", 0) or 0)
                ram_used = float(row.get("ram_used_gb", 0) or 0)
                ram_total = float(row.get("ram_total_gb", 0) or 0)
                net_recv = float(row.get("net_recv_mbps", 0) or 0)
                net_send = float(row.get("net_send_mbps", 0) or 0)
                rows.append({
                    "time": str(row.get("timestamp", "")),
                    "cpu": cpu,
                    "ram_used": ram_used,
                    "ram_total": ram_total,
                    "net_recv": net_recv,
                    "net_send": net_send,
                })
                cpu_vals.append(cpu)
                ram_vals.append(ram_used)
                net_recv_vals.append(net_recv)
                net_send_vals.append(net_send)

        _cpu_sorted = sorted(cpu_vals)
        _p99_idx = int(len(_cpu_sorted) * 0.99) if len(_cpu_sorted) >= 10 else -1
        server = {
            "rows": rows,
            "avg_cpu": round(_avg(cpu_vals), 2),
            "max_cpu": round(max(cpu_vals), 2) if cpu_vals else 0.0,
            "p99_cpu": round(_cpu_sorted[_p99_idx], 2) if cpu_vals else 0.0,
            "avg_ram_gb": round(_avg(ram_vals), 2),
            "total_ram_gb": round(rows[0]["ram_total"], 0) if rows else None,
            "avg_net_recv_mbps": round(_avg(net_recv_vals), 2),
            "avg_net_send_mbps": round(_avg(net_send_vals), 2),
        }

    # --- Process Spikes ---
    if server and process_spikes_csv_path:
        try:
            spike_rows = []
            with open(process_spikes_csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    spike_rows.append({
                        "time":          str(row.get("timestamp", "")),
                        "server_cpu":    float(row.get("server_cpu_percent", 0) or 0),
                        "process":       str(row.get("process_name", "")),
                        "pid":           str(row.get("pid", "")),
                        "proc_cpu":      float(row.get("proc_cpu_percent", 0) or 0),
                        "working_set_mb": float(row.get("working_set_mb", 0) or 0),
                        "cmd_line":      str(row.get("cmd_line", "") or ""),
                    })
            server["process_spikes"] = spike_rows
        except (OSError, KeyError):
            pass

    # --- Per-process time-series (SRS + ffmpeg) ---
    if server and proc_series_csv_path:
        try:
            ps_rows = []
            with open(proc_series_csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    ps_rows.append({
                        "time":          str(row.get("timestamp", "")),
                        "srs_cpu":       float(row.get("srs_cpu", 0) or 0),
                        "srs_ram_mb":    float(row.get("srs_ram_mb", 0) or 0),
                        "srs_virt_mb":   float(row.get("srs_virt_mb", 0) or 0),
                        "ffmpeg_count":  int(float(row.get("ffmpeg_count", 0) or 0)),
                        "ffmpeg_cpu":    float(row.get("ffmpeg_cpu", 0) or 0),
                        "ffmpeg_ram_mb": float(row.get("ffmpeg_ram_mb", 0) or 0),
                        "ffmpeg_virt_mb":float(row.get("ffmpeg_virt_mb", 0) or 0),
                    })
            server["proc_series"] = ps_rows
        except (OSError, KeyError, ValueError):
            pass

    # --- Per-ffmpeg instance stats grouped by stream URL ---
    if server and ffmpeg_instances_csv_path:
        try:
            from collections import defaultdict
            url_stats: dict = defaultdict(lambda: {
                "samples": 0, "cpu_sum": 0.0, "cpu_max": 0.0,
                "ram_sum": 0.0, "ram_max": 0.0,
                "virt_sum": 0.0, "virt_max": 0.0, "pids": set(),
            })
            with open(ffmpeg_instances_csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    url  = str(row.get("url", "") or "").strip() or "(no url)"
                    # Strip embedded credentials from URL (safety net — should already be stripped by collector)
                    import re as _re_cred
                    url = _re_cred.sub(r'(?<=://)([^@]+@)', '', url)
                    cpu  = float(row.get("cpu_pct",  0) or 0)
                    ram  = float(row.get("ram_mb",   0) or 0)
                    virt = float(row.get("virt_mb",  0) or 0)
                    idx  = str(row.get("stream_idx", row.get("pid", "")))
                    s = url_stats[url]
                    s["samples"] += 1
                    s["cpu_sum"]  += cpu
                    s["cpu_max"]  = max(s["cpu_max"], cpu)
                    s["ram_sum"]  += ram
                    s["ram_max"]  = max(s["ram_max"], ram)
                    s["virt_sum"] += virt
                    s["virt_max"] = max(s["virt_max"], virt)
                    s["pids"].add(idx)
            result = []
            for url, s in sorted(url_stats.items(), key=lambda x: -x[1]["cpu_max"]):
                n = s["samples"]
                result.append({
                    "url":         url,
                    "pid_count":   len(s["pids"]),
                    "samples":     n,
                    "avg_cpu_pct": round(s["cpu_sum"] / n, 2) if n else 0,
                    "max_cpu_pct": round(s["cpu_max"], 2),
                    "avg_ram_mb":  round(s["ram_sum"] / n, 1) if n else 0,
                    "max_ram_mb":  round(s["ram_max"], 1),
                    "avg_virt_mb": round(s["virt_sum"] / n, 1) if n else 0,
                    "max_virt_mb": round(s["virt_max"], 1),
                })
            server["ffmpeg_instances"] = result
        except (OSError, KeyError, ValueError):
            pass

    return {
        "scenario": scenario_name,
        "generated_at": now,
        "summary": summary,
        "sessions": sessions,
        "webrtc": webrtc,
        "source_breakdown": source_breakdown,
        "api": api,
        "server": server,
    }
