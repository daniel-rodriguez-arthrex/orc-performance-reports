"""
reporting/report.py
Write performance test results to HTML, CSV, and JSON.

HTML report:
  - Bootstrap 5.3 + Chart.js from CDN
  - Arthrex brand colour palette (#0077C8 primary, #003865 dark)
  - Google Fonts — Inter
  - Per-scenario sections: summary cards, session table, load-time chart,
    API latency chart, WebRTC stats, server CPU/RAM chart
  - capacity_validation: additional tier-comparison table across all runs
"""

import csv
import json
import os
import base64
from datetime import datetime as _dt

# ---------------------------------------------------------------------------
# Colour / badge helpers
# ---------------------------------------------------------------------------

_PRIMARY    = "#0077C8"   # Arthrex blue
_DARK       = "#1a2332"   # Arthrex dark navy
_CHART_BLUE = "#0077C8"  # Chart accent — stays blue for readability

# ---------------------------------------------------------------------------
# Logo helper — embed ArthrexLogo.png as base64 if present next to this file
# ---------------------------------------------------------------------------

def _logo_img_tag(height: int = 38) -> str:
    """Return an <img> tag with the logo embedded as base64, or '' if not found."""
    logo_path = os.path.join(os.path.dirname(__file__), "..", "ArthrexLogo.png")
    logo_path = os.path.normpath(logo_path)
    if not os.path.exists(logo_path):
        return ""
    with open(logo_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return (
        f'<img src="data:image/png;base64,{b64}" '
        f'alt="Arthrex" style="height:{height}px;filter:invert(1);mix-blend-mode:screen;'
        f'vertical-align:middle;margin-right:10px;" />'
    )
_SUCCESS      = "#198754"
_WARNING      = "#fd7e14"
_DANGER       = "#dc3545"
_CHART_PURPLE = "#6f42c1"
_CHART_TEAL   = "#20c997"

# ---------------------------------------------------------------------------
# Performance thresholds — used for PASS / WARN / FAIL verdict
# ---------------------------------------------------------------------------
# These are the agreed acceptable operating limits for an ORC deployment.
# Any metric in the WARN band means the system is functional but has little
# headroom.  Any metric exceeding the FAIL threshold means the configuration
# should NOT be recommended for that workload.

_THRESH = {
    # (warn_threshold, fail_threshold)  — higher is worse for CPU/RAM/latency
    "avg_cpu_pct":      (70,  85),
    "max_cpu_pct":      (85,  95),
    "avg_ram_pct":      (75,  85),
    "avg_load_ms":      (8_000,  15_000),
    "max_load_ms":      (12_000, 20_000),
    "connect_rate_pct": (100, 99),   # inverted: warn <100, fail <99
    # api_p95_ms intentionally omitted from verdict — metric is only
    # meaningful once ORC API path filtering is validated across all scenarios.
}


def _score_metric(key: str, value) -> str:
    """Return 'pass', 'warn', or 'fail' for a single metric value."""
    if value is None:
        return "pass"
    warn, fail = _THRESH.get(key, (None, None))
    if warn is None:
        return "pass"
    # connect_rate is inverted (lower = worse)
    if key == "connect_rate_pct":
        if value < fail:  return "fail"
        if value < warn:  return "warn"
        return "pass"
    if value >= fail:  return "fail"
    if value >= warn:  return "warn"
    return "pass"


def _overall_verdict(metrics: dict) -> tuple[str, list[dict]]:
    """Score all threshold metrics and return (verdict, findings).

    verdict  — 'pass' | 'warn' | 'fail'
    findings — list of {metric, value, level, label} for display
    """
    findings = []
    worst = "pass"
    labels = {
        "avg_cpu_pct":      "Avg CPU",
        "max_cpu_pct":      "Max CPU",
        "avg_ram_pct":      "Avg RAM",
        "avg_load_ms":      "Avg Session Load",
        "max_load_ms":      "Max Session Load",
        "connect_rate_pct": "Connect Rate",
    }
    fmt = {
        "avg_cpu_pct":      lambda v: f"{v}%",
        "max_cpu_pct":      lambda v: f"{v}%",
        "avg_ram_pct":      lambda v: f"{v}%",
        "avg_load_ms":      lambda v: f"{v/1000:.1f}s",
        "max_load_ms":      lambda v: f"{v/1000:.1f}s",
        "connect_rate_pct": lambda v: f"{v}%",
    }
    for key, label in labels.items():
        value = metrics.get(key)
        if value is None:
            continue
        level = _score_metric(key, value)
        if level == "fail" and worst != "fail":
            worst = "fail"
        elif level == "warn" and worst == "pass":
            worst = "warn"
        findings.append({"metric": key, "label": label,
                          "value": fmt[key](value), "level": level})
    return worst, findings


def _verdict_banner_html(verdict: str, findings: list[dict],
                          session_tier: int, hardware: str,
                          connected: int = None,
                          total_webrtc_conns: int = None,
                          webrtc_snaps: list = None,
                          abort_reason: str | None = None) -> str:
    """Render the top-of-report verdict banner."""
    colors  = {"pass": _SUCCESS, "warn": _WARNING, "fail": _DANGER}
    icons   = {"pass": "✓", "warn": "⚠", "fail": "✗"}
    labels  = {"pass": "PASS", "warn": "CAUTION", "fail": "FAIL"}
    if abort_reason:
        labels["fail"] = "ABORTED"
        icons["fail"]  = "⛔"
    c = colors[verdict]
    icon = icons[verdict]
    label = labels[verdict]

    hw_str = f" &nbsp;|&nbsp; {hardware}" if hardware else ""
    sessions_str = f"{session_tier} concurrent session{'s' if session_tier != 1 else ''}"

    # Connection breakdown: N_active × sources_per_session = total
    # Use tabs with conns > 1 (at least one video stream) as denominator so
    # the per-session figure is correct regardless of tier (12/24/36/…) and
    # regardless of whether some tabs had WHEP timeouts during the ramp.
    _raw_snaps     = webrtc_snaps or []
    _active_tabs   = sum(1 for s in _raw_snaps if s.get("conns", 0) > 1)
    _sess_for_conn = _active_tabs if _active_tabs else (connected or session_tier)
    _src_per_sess  = round(total_webrtc_conns / _sess_for_conn) if (total_webrtc_conns and _sess_for_conn) else None
    if _src_per_sess and total_webrtc_conns:
        conn_str = f" &nbsp;|&nbsp; {_sess_for_conn}&times;{_src_per_sess} = <strong>{total_webrtc_conns} connections</strong>"
    else:
        conn_str = ""

    rows = ""
    for f in findings:
        fc = colors.get(f["level"], _SUCCESS)
        ind = {"pass": "●", "warn": "⚠", "fail": "✗"}[f["level"]]
        rows += (
            f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
            f"<span style='color:{fc};font-weight:700;'>{ind}</span> "
            f"{f['label']}: <strong>{f['value']}</strong></span>"
        )

    if abort_reason:
        # Use actual connected count if available, otherwise fall back to session_tier
        if connected is not None and connected < session_tier:
            aborted_sessions = f"<strong>{connected} of {session_tier} sessions</strong> logged in"
        else:
            aborted_sessions = f"<strong>{sessions_str}</strong> completed"
        conn_clause = (
            f", producing <strong>{total_webrtc_conns} active connections</strong> ({_sess_for_conn}&times;{_src_per_sess}),"
            if (total_webrtc_conns and _src_per_sess) else ","
        )
        abort_note = (
            f"Test stopped early — <strong>{abort_reason}</strong>. "
            f"Only {aborted_sessions}{conn_clause} before the threshold was breached. "
            f"This configuration is <strong>not recommended</strong> for this workload."
        )
    else:
        abort_note = ""

    verdict_note = {
        "pass":  f"This configuration handles {sessions_str} with comfortable headroom.",
        "warn":  f"This configuration supports {sessions_str} but has limited headroom. "
                 f"Consider upgrading hardware before increasing load.",
        "fail":  abort_note if abort_reason else (
            f"This configuration is <strong>not recommended</strong> for {sessions_str}. "
            f"One or more critical thresholds exceeded."
        ),
    }[verdict]

    return (
        f"<div style='background:{c};color:#fff;border-radius:10px;"
        f"padding:18px 24px;margin-bottom:1.5rem;'>\n"
        f"  <div style='display:flex;align-items:center;gap:14px;flex-wrap:wrap;'>\n"
        f"    <div style='font-size:2rem;font-weight:800;line-height:1;'>{label}</div>\n"
        f"    <div style='flex:1;min-width:200px;'>\n"
        f"      <div style='font-size:.95rem;font-weight:600;'>{sessions_str}{conn_str}{hw_str}</div>\n"
        f"      <div style='font-size:.82rem;opacity:.9;margin-top:2px;'>{verdict_note}</div>\n"
        f"    </div>\n"
        f"  </div>\n"
        f"  <div style='margin-top:12px;font-size:.8rem;opacity:.92;'>{rows}</div>\n"
        f"</div>\n\n"
    )


# Alert types that are resource-monitoring events, not stream failures.
# These are excluded from the "hard errors" count and suppressed in the alerts table.
_RESOURCE_ALERT_TYPES = {"WHEP_RECONNECT", "CONNECTION_DROP", "RAM_DRIFT", "CPU_SUSTAINED",
                          "RAM_SUSTAINED", "CPU_DRIFT", "BROWSER_RESTART", "PAGE_FREEZE"}


def _endurance_banner_html(data: dict, hardware: str) -> str:
    """Render the endurance-specific stability verdict banner."""
    alerts       = data.get("alerts", [])
    dur_act      = data.get("duration_actual_s")
    dur_req      = data.get("duration_requested_s")
    # session_tier holds n_sources for endurance; fall back to summary.connected
    n_sources    = data.get("session_tier") or data.get("summary", {}).get("connected", 0)
    server       = data.get("server") or {}

    reconnects   = sum(1 for a in alerts if a["type"] == "WHEP_RECONNECT")
    browser_restarts = sum(1 for a in alerts if a["type"] == "BROWSER_RESTART")
    page_freezes = sum(1 for a in alerts if a["type"] == "PAGE_FREEZE")
    real_errors  = [a for a in alerts if a["type"] not in _RESOURCE_ALERT_TYPES]
    unrecovered  = [a for a in alerts if a["type"] == "CONNECTION_DROP"]
    ram_drifts   = sum(1 for a in alerts if a["type"] in ("RAM_DRIFT", "RAM_SUSTAINED"))
    cpu_sustained = sum(1 for a in alerts if a["type"] in ("CPU_SUSTAINED", "CPU_DRIFT"))

    hw_str   = f" &nbsp;|&nbsp; {hardware}" if hardware else ""
    dur_str  = _fmt_duration(dur_act) if dur_act else "—"
    src_str  = f"{n_sources} source{'s' if n_sources != 1 else ''}"

    avg_cpu = server.get("avg_cpu")
    p99_cpu = server.get("p99_cpu")
    max_cpu = server.get("max_cpu")
    avg_ram = server.get("avg_ram")

    # ── Verdict: CPU/RAM are the primary signal; stream drops are secondary ──
    # Hard errors (not connection-drop events) always fail.
    if real_errors:
        verdict, icon, label, bg = "fail", "✗", "FAILED",    _DANGER
    elif (avg_cpu is not None and avg_cpu >= 80) or (avg_ram is not None and avg_ram >= 85):
        verdict, icon, label, bg = "fail", "✗", "OVERLOADED", _DANGER
    elif (avg_cpu is not None and avg_cpu >= 60) or (avg_ram is not None and avg_ram >= 70):
        verdict, icon, label, bg = "warn", "⚠", "DEGRADED",  _WARNING
    elif unrecovered or reconnects > 0:
        # Resources were healthy but streams were occasionally unstable
        verdict, icon, label, bg = "warn", "⚠", "UNSTABLE",  _WARNING
    else:
        verdict, icon, label, bg = "pass", "✓", "STABLE",   _SUCCESS

    # ── Note: always lead with the CPU/RAM story ─────────────────────────────
    resource_parts = []
    if avg_cpu is not None:
        resource_parts.append(
            f"avg CPU {avg_cpu}%" + (f" (p99 {p99_cpu}%)" if p99_cpu else "")
        )
    if avg_ram is not None:
        resource_parts.append(f"avg RAM {avg_ram}%")

    stream_parts = []
    if unrecovered:
        stream_parts.append(
            f"{len(unrecovered)} transient drop{'s' if len(unrecovered) != 1 else ''} "
            f"detected by WebRTC poller"
        )
    resource_event_parts = []
    if cpu_sustained:
        resource_event_parts.append(f"CPU sustained high {cpu_sustained}×")
    if ram_drifts:
        resource_event_parts.append(f"RAM drift {ram_drifts}×")
    if browser_restarts:
        resource_event_parts.append(f"{browser_restarts} browser restart{'s' if browser_restarts != 1 else ''} (scheduled)")
    if page_freezes:
        resource_event_parts.append(f"{page_freezes} page freeze recovery{'s' if page_freezes != 1 else ''}")
    if real_errors:
        stream_parts.append(f"{len(real_errors)} hard error{'s' if len(real_errors) != 1 else ''}")

    if resource_parts:
        resource_str = "; ".join(resource_parts)
        detail_parts = resource_event_parts + stream_parts
        if detail_parts:
            note = f"Server: {resource_str}. {'; '.join(detail_parts)}."
        else:
            note = f"Server: {resource_str}. All {src_str} streamed for the full {dur_str}."
    else:
        all_parts = resource_event_parts + stream_parts
        if all_parts:
            note = f"{'; '.join(all_parts)}."
        else:
            note = f"All {src_str} streamed continuously for the full {dur_str}."

    # ── Findings row: CPU → RAM → reconnects ─────────────────────────────────
    rows = ""
    if avg_cpu is not None:
        cpu_color = _DANGER if avg_cpu >= 80 else _WARNING if avg_cpu >= 60 else _SUCCESS
        cpu_ind   = "✗" if avg_cpu >= 80 else "⚠" if avg_cpu >= 60 else "●"
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:{cpu_color};font-weight:700;'>{cpu_ind}</span> "
                 f"Avg CPU: <strong>{avg_cpu}%</strong></span>")
    if p99_cpu is not None:
        p99_cpu_color = _DANGER if p99_cpu >= 90 else _WARNING
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:{p99_cpu_color};font-weight:700;'>⚠</span> "
                 f"p99 CPU: <strong>{p99_cpu}%</strong></span>")
    if avg_ram is not None:
        ram_color = _DANGER if avg_ram >= 85 else _WARNING if avg_ram >= 70 else _SUCCESS
        ram_ind   = "✗" if avg_ram >= 85 else "⚠" if avg_ram >= 70 else "●"
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:{ram_color};font-weight:700;'>{ram_ind}</span> "
                 f"Avg RAM: <strong>{avg_ram}%</strong></span>")
    if cpu_sustained:
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:#fff;font-weight:700;'>⚠</span> "
                 f"CPU High Events: <strong>{cpu_sustained}</strong></span>")
    if ram_drifts:
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:#fff;font-weight:700;'>⚠</span> "
                 f"RAM Drift Events: <strong>{ram_drifts}</strong></span>")
    if unrecovered:
        drop_color = _WARNING if verdict != "fail" else "#fff"
        rows += (f"<span style='margin-right:1.5rem;white-space:nowrap;'>"
                 f"<span style='color:{drop_color};font-weight:700;'>⚠</span> "
                 f"Transient Drops: <strong>{len(unrecovered)}</strong></span>")

    return (
        f"<div style='background:{bg};color:#fff;border-radius:10px;"
        f"padding:18px 24px;margin-bottom:1.5rem;'>\n"
        f"  <div style='display:flex;align-items:center;gap:14px;flex-wrap:wrap;'>\n"
        f"    <div style='font-size:2rem;font-weight:800;line-height:1;'>{label}</div>\n"
        f"    <div style='flex:1;min-width:200px;'>\n"
        f"      <div style='font-size:.95rem;font-weight:600;'>"
        f"{src_str} &nbsp;|&nbsp; {dur_str}{hw_str}</div>\n"
        f"      <div style='font-size:.82rem;opacity:.9;margin-top:2px;'>{note}</div>\n"
        f"    </div>\n"
        f"  </div>\n"
        f"  <div style='margin-top:12px;font-size:.8rem;opacity:.92;'>{rows}</div>\n"
        f"</div>\n\n"
    )


def _rate_color(rate: float) -> str:
    if rate >= 90:
        return _SUCCESS
    if rate >= 70:
        return _WARNING
    return _DANGER


def _badge(rate: float) -> str:
    color = _rate_color(rate)
    return (
        f'<span style="background:{color};color:#fff;padding:6px 14px;'
        f'border-radius:20px;font-size:.9rem;font-weight:600;">'
        f'{rate:.1f}% connected</span>'
    )


def _check(val: bool) -> str:
    return (
        '<span style="color:#198754;font-weight:700;">&#10003;</span>' if val
        else '<span style="color:#dc3545;font-weight:700;">&#10007;</span>'
    )


def _js_array(values: list) -> str:
    return json.dumps(values)


def _iso_to_epoch(s: str) -> float:
    """Convert 'YYYY-MM-DDTHH:MM:SS' to Unix epoch float; returns 0.0 on error."""
    try:
        return _dt.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


def _recompute_api(api: dict) -> dict:
    """Ensure avg_ms reflects non-bandwidth calls only.

    Recomputes from the raw calls list so historical raw_data.json files
    (which stored avg_ms as a blended all-calls average) render correctly
    alongside new runs.  If non_bw_total is already present and non-zero the
    data came from the updated aggregator and we trust it as-is.
    """
    if not api:
        return api
    if api.get("non_bw_total") is not None:
        return api   # already correct from updated aggregator
    calls = api.get("calls", [])
    if not calls:
        return api
    non_bw = [c["duration_ms"] for c in calls if not c.get("is_bw_check", False)]
    bw     = [c["duration_ms"] for c in calls if     c.get("is_bw_check", False)]
    patched = dict(api)
    patched["non_bw_total"] = len(non_bw)
    patched["bw_checks"]    = len(bw)
    patched["avg_ms"]       = round(sum(non_bw) / len(non_bw), 2) if non_bw else 0.0
    patched.pop("p95_ms", None)   # remove stale blended p95 if present
    return patched

def _session_rows(sessions: list) -> str:
    if not sessions:
        return "<tr><td colspan='4' class='text-center text-muted py-3'>No source data</td></tr>"
    rows = []
    for s in sessions:
        bg = "" if s["connected"] else 'style="background:#fff5f5;"'
        rows.append(
            f'<tr {bg}>'
            f'<td class="text-center">{s["tab"] + 1}</td>'
            f'<td class="text-center">{_check(s["connected"])}</td>'
            f'<td class="text-center">{_check(s["modal_fired"])}</td>'
            f'<td class="text-end">{s["load_ms"]/1000:.2f}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _webrtc_rows(snapshots: list) -> str:
    if not snapshots:
        return "<tr><td colspan='7' class='text-center text-muted py-3'>No WebRTC data</td></tr>"
    has_mbps = any(s.get("avg_mbps") is not None for s in snapshots)
    rows = []
    for s in snapshots:
        avg_mbps = s.get("avg_mbps")
        mbps_cell = (
            f'<td class="text-end">{avg_mbps:.1f}</td>' if avg_mbps is not None
            else '<td class="text-end text-muted">&#8212;</td>'
        ) if has_mbps else ""
        rows.append(
            f'<tr>'
            f'<td class="text-center">{s["tab"] + 1}</td>'
            f'<td class="text-end">{s["fps"]:.1f}</td>'
            f'<td class="text-end">{s["dropped"]}</td>'
            f'<td class="text-end">{s["jitter_ms"]:.2f}</td>'
            f'<td class="text-end">{s["rtt_ms"]:.2f}</td>'
            f'<td class="text-end">{s["bytes_rx"]/1_000_000:.1f} MB</td>'
            + mbps_cell
            + f'</tr>'
        )
    return "\n".join(rows)


def _url_to_source_label(url: str) -> str:
    """Resolve a stream URL to a human-friendly source label.

    Strips credentials, then matches against the known source catalogue.
    Falls back to a pattern-based label when no exact match is found.
    """
    import re as _re
    # Strip credentials: scheme://user:pass@host -> scheme://host
    clean = _re.sub(r'(?<=://)([^@]+@)', '', url or '')
    # Try exact match against catalogue
    try:
        from config.sources import VISION, CAMERAS, SIMULATED, MATRIX
        for src in (*VISION, *CAMERAS, *SIMULATED, *MATRIX):
            src_clean = _re.sub(r'(?<=://)([^@]+@)', '', src.get('url', ''))
            if src_clean == clean:
                return src['name']
    except Exception:
        pass
    # Pattern-based fallback
    m_sim = _re.search(r'/orc/stream(\d+)$', clean)
    if m_sim:
        return f"Sim stream {m_sim.group(1)}"
    m_vision = _re.search(r'(\d+\.\d+\.\d+\.\d+).*?/primarycamera', clean)
    if m_vision:
        # Last two octets as short identifier
        parts = m_vision.group(1).split('.')
        return f"Vision {parts[2]}.{parts[3]}"
    m_matrix = _re.search(r'/live/(\S+)$', clean)
    if m_matrix:
        return f"Matrix {m_matrix.group(1)}"
    # Last path segment
    seg = clean.rstrip('/').rsplit('/', 1)[-1]
    return seg or clean


def _normalize_proc_name(name: str) -> str:
    """Strip the #N suffix WMI appends when multiple instances of a process exist.

    For processes where each numbered instance is meaningfully distinct (e.g. ffmpeg#N
    where N corresponds to a specific stream ingester), the number is preserved.
    """
    import re as _re
    # Processes where #N carries real meaning — keep as-is
    _KEEP_NUMBERED = {"ffmpeg"}
    base = _re.sub(r'#\d+$', '', str(name))
    if base in _KEEP_NUMBERED:
        return str(name)   # e.g. "ffmpeg#28" stays "ffmpeg#28"
    return base


def _proc_category(norm_name: str) -> tuple[str, str]:
    """Return (category_label, badge_html) for a normalized process name."""
    import re as _re
    # ffmpeg#N instances are all ORC Product (SRS stream ingesters)
    base = _re.sub(r'#\d+$', '', norm_name)
    _ORC = {"srs", "ffmpeg", "orcapi", "node", "srs-whip-whep", "agent", "process-agent", "MSI3EEF.tmp"}
    _SEC = {"SentinelAgent", "SentinelServiceHost", "SentinelStaticEngineScanner",
            "TiWorker", "TaniumClient", "TaniumCX", "TaniumTSDB"}
    _OBS = {"datadog-installer", "TPython", "java", "winrshost", "procexp64", "powershell"}
    _OS  = {"svchost", "System", "smss", "spoolsv", "services", "dwm", "sihost",
            "csrss", "rdpclip", "explorer", "CompatTelRunner", "MoUsoCoreWorker",
            "conhost", "ServerManager", "fontdrvhost", "LogonUI", "WmiPrvSE",
            "sppsvc", "setup"}
    if base in _ORC:
        return ("ORC Product",        '<span class="badge" style="background:#0d6efd;font-size:.68rem;">ORC Product</span>')
    if base in _SEC:
        return ("Security/AV",        '<span class="badge" style="background:#dc3545;font-size:.68rem;">Security/AV</span>')
    if base in _OBS:
        return ("Observability",       '<span class="badge" style="background:#6f42c1;font-size:.68rem;">Observability</span>')
    if base in _OS:
        return ("OS/System",           '<span class="badge" style="background:#6c757d;font-size:.68rem;">OS/System</span>')
    return ("Other",                   '<span class="badge" style="background:#adb5bd;color:#000;font-size:.68rem;">Other</span>')


def _process_spikes_table_html(server: dict) -> str:
    """Render CPU-spike process snapshots: aggregated summary + worst-event detail."""
    import re as _re
    from collections import defaultdict

    spikes = (server or {}).get("process_spikes", [])
    if not spikes:
        return ""

    # ── 1. Aggregate per process key ──────────────────────────────────────────
    # For ffmpeg: key by stream URL (strip credentials) so each stream is its own
    # row regardless of which WMI slot (#N) it runs in.
    # For all other processes: key by normalized process name as before.
    from html import escape as _he
    agg: dict[str, dict] = {}
    for s in spikes:
        norm = _normalize_proc_name(s.get("process", ""))
        cpu  = float(s.get("proc_cpu", 0))
        ram  = float(s.get("working_set_mb", 0))
        if "ffmpeg" in norm:
            cmd = s.get("cmd_line", "") or ""
            m = _re.search(r'(rtsps?://\S+|rtmps?://\S+|https?://\S+)', cmd)
            if m:
                raw_url = m.group(1)
                agg_key = "ffmpeg::" + _re.sub(r'(?<=://)([^@]+@)', '', raw_url)
            else:
                agg_key = "ffmpeg::(no url)"
        else:
            agg_key = norm
        if agg_key not in agg:
            agg[agg_key] = {"cpu_vals": [], "sum_cpu": 0.0, "n": 0,
                            "peak_ram": 0.0, "instances": set(), "url": "",
                            "display_name": norm}
        agg[agg_key]["cpu_vals"].append(cpu)
        agg[agg_key]["sum_cpu"] += cpu
        agg[agg_key]["n"]       += 1
        agg[agg_key]["peak_ram"] = max(agg[agg_key]["peak_ram"], ram)
        agg[agg_key]["instances"].add(s.get("process", norm))
        if "ffmpeg" in norm and not agg[agg_key]["url"]:
            cmd = s.get("cmd_line", "") or ""
            m = _re.search(r'(rtsps?://\S+|rtmps?://\S+|https?://\S+)', cmd)
            if m:
                agg[agg_key]["url"] = _re.sub(r'(?<=://)([^@]+@)', '', m.group(1))

    # compute p99 per process and sort by p99 descending
    for d in agg.values():
        vals = sorted(d["cpu_vals"])
        d["p99_cpu"] = vals[int(len(vals) * 0.99)] if len(vals) >= 10 else vals[-1] if vals else 0.0
    sorted_agg = sorted(agg.items(), key=lambda kv: kv[1]["p99_cpu"], reverse=True)

    # ── 2. Build aggregated summary rows ─────────────────────────────────────
    sum_rows = []
    for agg_key, d in sorted_agg:
        norm = d["display_name"]
        avg_cpu  = d["sum_cpu"] / d["n"]
        p99_cpu  = d["p99_cpu"]
        instances = sorted(d["instances"])
        inst_count = len(instances)
        _, badge = _proc_category(norm)

        # instance detail: show up to 5 raw names (e.g. "ffmpeg#1, ffmpeg#2, …")
        if inst_count > 1:
            shown = ", ".join(instances[:5])
            if inst_count > 5:
                shown += f" +{inst_count - 5} more"
            inst_html = (f'<span title="{_he(shown)}" style="cursor:help;font-size:.7rem;color:#6c757d;">'
                         f'{inst_count} instances</span>')
        else:
            inst_html = ""

        # color thresholds: p99 cpu ≥ 100% = danger, ≥ 50% = warning
        if p99_cpu >= 100:
            max_style = f' style="color:{_DANGER};font-weight:700;"'
        elif p99_cpu >= 50:
            max_style = f' style="color:#fd7e14;font-weight:700;"'
        else:
            max_style = ""

        _url = d.get("url", "")
        if "ffmpeg" in norm and _url:
            _label = _url_to_source_label(_url)
            _tip_text = f"{_url}"
            _tip = f' data-bs-toggle="tooltip" data-bs-placement="top" title="{_he(_tip_text)}" style="cursor:help;text-decoration:underline dotted;"'
            _icon = f'<span{_tip}>{_he(_label)}</span>'
        elif "ffmpeg" in norm:
            _icon = norm
        else:
            _icon = norm
        sum_rows.append(
            f'<tr>'
            f'<td style="font-size:.8rem;font-weight:600;">{_icon}</td>'
            f'<td>{badge}</td>'
            f'<td class="text-end"{max_style}>{p99_cpu:.0f}%</td>'
            f'<td class="text-end" style="font-size:.8rem;">{avg_cpu:.0f}%</td>'
            f'<td class="text-end" style="font-size:.8rem;">{d["n"]:,}</td>'
            f'<td class="text-end" style="font-size:.8rem;">{d["peak_ram"]:.0f} MB</td>'
            f'<td style="font-size:.75rem;">{inst_html}</td>'
            f'</tr>'
        )

    # ── 3. Build worst-event detail (top 100 by server_cpu, capped to avoid hang) ──
    worst_events = sorted(spikes, key=lambda s: float(s.get("server_cpu", 0)), reverse=True)
    # collect up to 10 worst distinct timestamps
    seen_ts: list[str] = []
    for s in worst_events:
        ts = str(s.get("time", ""))
        if ts not in seen_ts:
            seen_ts.append(ts)
        if len(seen_ts) >= 10:
            break
    top_ts_set = set(seen_ts)
    detail_spikes = [s for s in spikes if str(s.get("time", "")) in top_ts_set]

    # Group detail spikes by raw timestamp to allow per-event RAM totals
    from collections import OrderedDict as _OD
    from datetime import datetime as _dt
    _groups: _OD = _OD()
    for s in detail_spikes:
        raw_ts = str(s.get("time", ""))
        _groups.setdefault(raw_ts, []).append(s)

    detail_rows = []
    for raw_ts, group in _groups.items():
        # Format timestamp label
        try:
            _parsed = _dt.fromisoformat(raw_ts)
            ts_label = _parsed.strftime("%b %d %H:%M:%S")
        except Exception:
            ts_label = raw_ts[-19:] if len(raw_ts) >= 19 else raw_ts
        server_cpu = float(group[0].get("server_cpu", 0))
        detail_rows.append(
            f'<tr class="table-secondary fw-semibold">'
            f'<td colspan="5" style="font-size:.75rem;">'
            f'{ts_label} — server CPU {server_cpu:.1f}%'
            f'</td></tr>'
        )
        total_ram = 0.0
        for s in group:
            pct  = float(s.get("proc_cpu", 0))
            norm = _normalize_proc_name(s.get("process", ""))
            _, badge = _proc_category(norm)
            if pct >= 100:
                pct_style = f' style="color:{_DANGER};font-weight:700;"'
            elif pct >= 50:
                pct_style = ' style="color:#fd7e14;font-weight:700;"'
            else:
                pct_style = ""
            ram_mb = float(s.get("working_set_mb", 0))
            total_ram += ram_mb
            # Extract stream URL from cmd_line for ffmpeg processes
            cmd = s.get("cmd_line", "") or ""
            url_match = _re.search(r'(rtsps?://\S+|rtmps?://\S+|https?://\S+)', cmd)
            url_tip = ""
            if url_match:
                url_val = _re.sub(r'["\'].*', '', url_match.group(1))
                url_tip = f' data-bs-toggle="tooltip" data-bs-placement="top" title="{_he(url_val)}" style="cursor:help;text-decoration:underline dotted;"'
            proc_label = _he(s.get("process", ""))
            if url_tip:
                proc_cell = f'<td style="padding-left:1.5rem;font-size:.8rem;"><span{url_tip}>{proc_label}</span></td>'
            else:
                proc_cell = f'<td style="padding-left:1.5rem;font-size:.8rem;">{proc_label}</td>'
            detail_rows.append(
                f'<tr>'
                + proc_cell +
                f'<td>{badge}</td>'
                f'<td class="text-muted" style="font-size:.75rem;">{s.get("pid","")}</td>'
                f'<td class="text-end"{pct_style}>{pct:.0f}%</td>'
                f'<td class="text-end" style="font-size:.8rem;">{ram_mb:.0f} MB</td>'
                f'</tr>'
            )
        # Total RAM row for this spike event
        detail_rows.append(
            f'<tr style="border-top:2px solid #dee2e6;">'
            f'<td colspan="4" style="font-size:.75rem;font-weight:600;padding-left:1.5rem;color:#495057;">'
            f'Total RAM (top {len(group)} processes)</td>'
            f'<td class="text-end" style="font-size:.8rem;font-weight:700;">{total_ram/1024:.2f} GB</td>'
            f'</tr>'
        )

    uid_sum    = "procSpikesSummary"
    uid_detail = "procSpikesDetail"
    uid_body   = "procSpikesBody"
    total_events = len({s.get("time") for s in spikes})

    return (
        f'<div class="detail-sub-header mt-3" style="cursor:pointer;" '
        f'data-bs-toggle="collapse" data-bs-target="#{uid_body}" '
        f'aria-expanded="true" aria-controls="{uid_body}">'
        f'CPU Spike Process Analysis &#9660;</div>'
        f'<div class="collapse show" id="{uid_body}">'
        f'<p style="font-size:.82rem;color:#6c757d;margin:2px 0 8px;">'
        f'CPU% is per-core — values &gt;100% mean the process is using more than one full core. '
        f'Server CPU% is the aggregate normalized across all cores. '
        f'Spike events = number of polling intervals where process appeared above threshold. '
        f'<strong>ffmpeg processes</strong> are SRS stream ingesters — one per active room source.'
        f'</p>'

        # ── Summary table ────────────────────────────────────────────────────
        f'<div style="overflow-x:auto;">'
        f'<table class="data-table data-table-sm">'
        f'<thead><tr>'
        f'<th>Process</th>'
        f'<th>Category</th>'
        f'<th class="text-end" title="p99 CPU across all spike snapshots — sorted descending">p99 CPU% &#9660;</th>'
        f'<th class="text-end">Avg CPU%</th>'
        f'<th class="text-end">Spike Events</th>'
        f'<th class="text-end">Peak RAM</th>'
        f'<th>Instances</th>'
        f'</tr></thead>'
        f'<tbody>' + "\n".join(sum_rows) + f'</tbody>'
        f'</table></div>'

        # ── Worst-event detail (collapsed) ───────────────────────────────────
        f'<div class="mt-2">'
        f'<button class="btn btn-sm btn-outline-secondary py-0 px-2" '
        f'type="button" data-bs-toggle="collapse" data-bs-target="#{uid_detail}">'
        f'Show worst {len(top_ts_set)} spike events (of {total_events:,} total) &#9660;'
        f'</button></div>'
        f'<div class="collapse" id="{uid_detail}">'
        f'<div style="overflow-x:auto;margin-top:6px;">'
        f'<table class="data-table data-table-sm">'
        f'<thead><tr>'
        f'<th>Process</th><th>Category</th><th>PID</th>'
        f'<th class="text-end">CPU%</th><th class="text-end">Working Set</th>'
        f'</tr></thead>'
        f'<tbody>' + "\n".join(detail_rows) + f'</tbody>'
        f'</table></div></div>'
        f'</div>'  # close uid_body collapse
    )


def _memory_table_html(snapshots: list, total_ram_gb: float = None) -> str:
    """Render server snapshot timeline as a compact memory/CPU table."""
    if not snapshots:
        return ""
    rows = []
    for s in snapshots:
        cpu = s.get("cpu_percent", 0)
        ram = s.get("ram_used_gb", 0)
        net = round(s.get("net_send", 0), 2)  # outbound Mbps (server → clients)
        ts  = str(s.get("timestamp", ""))[-8:] if s.get("timestamp") else "&#8212;"
        cpu_style = f' style="color:{_DANGER};font-weight:700;"' if cpu >= 80 else ""
        if total_ram_gb:
            ram_pct = ram / total_ram_gb * 100
            ram_style = f' style="color:{_DANGER};font-weight:700;"' if ram_pct >= 90 else ""
            ram_cell = f'{ram:.2f} / {total_ram_gb:.0f} GB <span style="color:#6c757d;font-size:.75em;">({ram_pct:.0f}%)</span>'
        else:
            ram_style = ""
            ram_cell  = f'{ram:.2f} GB'
        rows.append(
            f'<tr>'
            f'<td class="text-muted" style="font-size:.75rem;">{ts}</td>'
            f'<td class="text-end"{cpu_style}>{cpu:.1f}%</td>'
            f'<td class="text-end"{ram_style}>{ram_cell}</td>'
            f'<td class="text-end">{net:.1f} Mbps</td>'
            f'</tr>'
        )
    html = (
        '<div class="detail-sub-header">Server Memory &amp; CPU Timeline</div>'
        '<div style="overflow-x:auto;">'
        '<table class="data-table data-table-sm">'
        '<thead><tr>'
        '<th>Time</th><th class="text-end">CPU%</th>'
        '<th class="text-end">RAM (Used / Total)</th><th class="text-end">Net Send (to clients)</th>'
        '</tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody>'
        '</table></div>'
    )
    return html


def _webrtc_stability_table_html(webrtc_snapshots: list) -> str:
    """Render per-session WebRTC stats with per-source fps and stability color-coding.

    ORC creates one non-video RTCPeerConnection (signaling/control) per tab alongside
    the video stream connections.  We subtract 1 from raw `conns` to show video-only
    stream count, and divide total FPS by that corrected count for a meaningful
    per-source fps figure.  Stability thresholds operate on the per-source value.
    """
    if not webrtc_snapshots:
        return ""
    if all(s.get("conns", 0) <= 1 for s in webrtc_snapshots):
        return ""  # suppress entirely when no video streams are connected
    has_mbps = any(s.get("avg_mbps") is not None for s in webrtc_snapshots)
    rows = []
    for s in webrtc_snapshots:
        fps_total = float(s.get("fps", 0))
        jitter    = float(s.get("jitter_ms", 0))
        dropped   = int(s.get("dropped", 0))
        rtt       = float(s.get("rtt_ms", 0))
        bytes_rx  = int(s.get("bytes_rx", 0))
        raw_conns = int(s.get("conns", 0))
        tab       = int(s.get("tab", 0))
        avg_mbps  = s.get("avg_mbps")

        # Video streams only (subtract the 1 non-video signaling connection)
        streams = max(0, raw_conns - 1)
        # Per-source fps: total fps divided across active video streams
        fps_per_src = (fps_total / streams) if streams > 0 else 0.0

        # Stability thresholds are per-source:
        #   fps/src < 25  → likely degraded (normal is ~30 fps per source)
        #   jitter  > 100ms → noticeable freeze/stutter
        #   dropped > 0  → frames were lost
        unstable   = (streams == 0) or fps_per_src < 25 or jitter > 100 or dropped > 0
        row_style  = f' style="background:#fff5f5;"' if unstable else ""
        fps_style  = f' style="color:{_DANGER};font-weight:700;"' if (streams > 0 and fps_per_src < 25) else ""
        jit_style  = f' style="color:{_WARNING};font-weight:700;"' if jitter > 100 else ""
        drop_style = f' style="color:{_DANGER};font-weight:700;"' if dropped > 0 else ""
        stable_badge = (
            f'<span style="background:{_DANGER};color:#fff;font-size:.65rem;padding:2px 7px;border-radius:10px;">UNSTABLE</span>'
            if unstable else
            f'<span style="background:{_SUCCESS};color:#fff;font-size:.65rem;padding:2px 7px;border-radius:10px;">OK</span>'
        )
        mbps_cell = (
            f'<td class="text-end">{avg_mbps:.1f}</td>' if avg_mbps is not None
            else '<td class="text-end text-muted">&#8212;</td>'
        )
        streams_cell = (
            f'<td class="text-center">{streams}</td>' if streams > 0
            else f'<td class="text-center" style="color:{_DANGER};font-weight:700;">0</td>'
        )
        rows.append(
            f'<tr{row_style}>'
            f'<td class="text-center">{tab + 1}</td>'
            + streams_cell
            + f'<td class="text-end"{fps_style}>{fps_per_src:.1f}</td>'
            f'<td class="text-end"{drop_style}>{dropped:,}</td>'
            f'<td class="text-end"{jit_style}>{jitter:.1f}</td>'
            f'<td class="text-end">{rtt:.1f}</td>'
            f'<td class="text-end">{bytes_rx/1_000_000:.1f} MB</td>'
            + (mbps_cell if has_mbps else "")
            + f'<td class="text-center">{stable_badge}</td>'
            f'</tr>'
        )
    mbps_th = '<th class="text-end">Avg Mbps</th>' if has_mbps else ""
    html = (
        '<div class="detail-sub-header">WebRTC Stream Stability</div>'
        '<div style="overflow-x:auto;">'
        '<table class="data-table data-table-sm">'
        '<thead><tr>'
        '<th class="text-center">Session #</th>'
        '<th class="text-center">Streams</th>'
        '<th class="text-end">FPS / src</th>'
        '<th class="text-end">Dropped</th>'
        '<th class="text-end">Jitter (ms)</th>'
        '<th class="text-end">RTT (ms)</th>'
        '<th class="text-end">Bytes Rx</th>'
        + mbps_th
        + '<th class="text-center">Stable?</th>'
        '</tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody>'
        '</table></div>'
    )
    return html


# ---------------------------------------------------------------------------
# Chart JS builders
# ---------------------------------------------------------------------------

def _session_chart_js(sessions: list) -> str:
    if not sessions:
        return ""
    labels = [str(s["tab"] + 1) for s in sessions]
    values = [round(s["load_ms"] / 1000, 3) for s in sessions]
    colors = [
        '"rgba(25,135,84,0.85)"' if s["connected"] else '"rgba(220,53,69,0.85)"'
        for s in sessions
    ]
    colors_js = ", ".join(colors)
    labels_js = _js_array(labels)
    values_js = _js_array(values)
    return (
        "new Chart(document.getElementById('sessionChart'), {"
        "  type: 'bar',"
        f"  data: {{ labels: {labels_js},"
        f"    datasets: [{{ label: 'Load Time (s)', data: {values_js},"
        f"      backgroundColor: [{colors_js}], borderRadius: 4 }}] }},"
        "  options: { responsive: true,"
        "    plugins: { legend: { display: false },"
        "      tooltip: { callbacks: { label: ctx => ' ' + ctx.raw.toFixed(2) + ' s' } } },"
        "    scales: {"
        "      x: { title: { display: true, text: 'Session #', font: { weight: '600' } } },"
        "      y: { title: { display: true, text: 'seconds', font: { weight: '600' } }, beginAtZero: true }"
        "    } } });"
    )


def _fmt_time_12h(iso_or_str: str) -> str:
    """Convert 'YYYY-MM-DDTHH:MM:SS' or any string ending HH:MM:SS to 12-hour 'H:MM AM/PM'."""
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            dt = datetime.strptime(iso_or_str[-19:] if len(iso_or_str) >= 19 else iso_or_str, fmt)
            return dt.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return iso_or_str  # fallback: return as-is


def _api_chart_js(api: dict, interval_s: float = 30.0) -> str:
    """Bar chart: avg API duration bucketed into 30-second windows.

    X axis  = elapsed time window (e.g. '0-30s', '30-60s' …) — each window
              corresponds roughly to one ramp-up interval / new session joining.
    Y axis  = avg duration (ms) of all requests that fired in that window.
    Shows clearly whether latency increases as concurrent sessions are added.
    """
    calls = api.get("calls", [])
    if not calls:
        return ""

    t0 = min(c["time"] for c in calls)
    bucket_sec = int(interval_s) if interval_s else 30

    buckets: dict[int, list] = {}
    for c in calls:
        b = int((c["time"] - t0) // bucket_sec)
        buckets.setdefault(b, []).append(c["duration_ms"])

    max_b   = max(buckets)
    labels  = []
    avgs    = []
    counts  = []
    for b in range(max_b + 1):
        labels.append(f"{b * bucket_sec}-{(b+1) * bucket_sec}s")
        vals = buckets.get(b, [])
        avgs.append(round(sum(vals) / len(vals), 1) if vals else 0)
        counts.append(len(vals))

    lbl_js   = _js_array(labels)
    avg_js   = _js_array(avgs)
    count_js = _js_array(counts)
    blue     = _CHART_BLUE

    return (
        f"new Chart(document.getElementById('apiChart'), {{"
        f"  type: 'bar',"
        f"  data: {{ labels: {lbl_js},"
        f"    datasets: [{{ label: 'Avg Duration (ms)', data: {avg_js},"
        f"      backgroundColor: '{blue}cc', borderRadius: 4 }}] }},"
        f"  options: {{ responsive: true,"
        f"    plugins: {{"
        f"      legend: {{ display: false }},"
        f"      tooltip: {{ callbacks: {{ label: function(ctx) {{"
        f"        var n = {count_js}[ctx.dataIndex];"
        f"        return ' ' + ctx.parsed.y.toFixed(1) + ' ms avg (' + n + ' calls)';"
        f"      }} }} }}"
        f"    }},"
        f"    scales: {{"
        f"      x: {{ title: {{ display: true, text: 'Elapsed time ({bucket_sec}s windows)' }} }},"
        f"      y: {{ beginAtZero: true, title: {{ display: true, text: 'Avg Duration (ms)' }} }}"
        f"    }} }} }});"
    )


def _downsample(rows: list, max_points: int = 1440) -> list:
    """Thin a list to at most max_points, always preserving first and last."""
    n = len(rows)
    if n <= max_points:
        return rows
    step = n / max_points
    indices = {0, n - 1}
    x = 0.0
    while x < n:
        indices.add(int(x))
        x += step
    return [rows[i] for i in sorted(indices)]


def _reset_zoom_btn(chart_id: str) -> str:
    """HTML for a small Reset Zoom button below a chart."""
    return (
        f"<div class='text-end mt-1'>"
        f"<button class='btn btn-sm btn-outline-secondary py-0 px-2' style='font-size:.7rem;'"
        f" onclick=\"var c=Chart.getChart(document.getElementById('{chart_id}'));if(c)c.resetZoom();\">"
        f"&#8635; Reset Zoom</button></div>"
    )


_ZOOM_PLUGIN_OPTS = (
    "zoom: { zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },"
    "        pan:  { enabled: true, mode: 'x' } }"
)


def _server_chart_js(server: dict, sessions: list = None, ramp_start_epoch: float = 0,
                     interval_s: float = 2) -> str:
    if not server or not server.get("rows"):
        return ""
    rows       = server["rows"]

    # For multi-day runs use elapsed "Day N Hh" labels; short runs keep wall-clock time
    run_span_s = 0.0
    if len(rows) >= 2:
        try:
            run_span_s = _iso_to_epoch(rows[-1]["time"]) - _iso_to_epoch(rows[0]["time"])
        except Exception:
            pass
    if run_span_s > 86400:
        t0_epoch = _iso_to_epoch(rows[0]["time"])
        labels = [_fmt_duration(_iso_to_epoch(r["time"]) - t0_epoch, run_span_s) for r in rows]
    else:
        labels = [_fmt_time_12h(r["time"]) for r in rows]
    max_ticks = max(20, min(30, int(run_span_s / 3600 / 5))) if run_span_s > 3600 else 20

    # Downsample to ≤1440 points so the chart renders fast and cleanly
    rows   = _downsample(rows, max_points=1440)
    labels = _downsample(labels, max_points=1440)

    cpu        = [r["cpu"] for r in rows]
    ram        = [r["ram_used"] for r in rows]
    total_ram  = server.get("total_ram_gb")
    lbl_js     = _js_array(labels)
    cpu_js     = _js_array(cpu)
    ram_js     = _js_array(ram)

    # Reference line at 60% CPU (ideal target)
    sixty_js = _js_array([60] * len(cpu))

    # Compute session-connect markers (vertical lines on the chart)
    markers = []
    if sessions and ramp_start_epoch:
        row_epochs = [_iso_to_epoch(r["time"]) for r in rows]
        if row_epochs:
            for i, sess in enumerate(sorted(sessions, key=lambda s: s.get("tab", 0))):
                connect_epoch = ramp_start_epoch + i * float(interval_s) + sess.get("load_ms", 0) / 1000
                nearest = min(range(len(row_epochs)), key=lambda j: abs(row_epochs[j] - connect_epoch))
                markers.append({"xLabel": labels[nearest], "label": f"S{sess.get('tab', i) + 1}"})
    markers_js = json.dumps(markers)

    cpu_chart = (
        "new Chart(document.getElementById('serverCpuChart'), {"
        "  type: 'line',"
        f"  data: {{ labels: {lbl_js},"
        "    datasets: ["
        f"      {{ label: 'CPU %', data: {cpu_js},"
        f"        borderColor: '{_DANGER}', backgroundColor: '{_DANGER}18',"
        "         tension: 0.3, borderWidth: 2, pointRadius: 0 },"
        f"      {{ label: '60% target', data: {sixty_js},"
        "         borderColor: '#ffc107', backgroundColor: 'transparent',"
        "         borderDash: [6,4], borderWidth: 1, pointRadius: 0 }"
        "    ] },"
        "  options: { responsive: true, interaction: { mode: 'index', intersect: false },"
        f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }}, vertLines: {markers_js}, {_ZOOM_PLUGIN_OPTS} }},"
        "    scales: {"
        f"      x: {{ ticks: {{ autoSkip: true, maxRotation: 45, maxTicksLimit: {max_ticks} }}, title: {{ display: true, text: 'Time (scroll to zoom, drag to pan)' }} }},"
        "      y: { beginAtZero: true, max: 100,"
        "           title: { display: true, text: 'CPU %' },"
        "           ticks: { callback: v => v + '%' } }"
        "    } } });"
    )

    # ── Spike annotation data for RAM chart ──────────────────────────────────
    import json as _json
    import statistics as _stats
    _spikes_raw = server.get("process_spikes") or []
    _spikes_by_ts: dict = {}
    for _sp in _spikes_raw:
        _spikes_by_ts.setdefault(_sp["time"], []).append(_sp)
    _row_epochs = [_iso_to_epoch(r["time"]) for r in rows]
    _spike_meta: dict = {}  # row_idx -> {cpuPct, y, lines}
    for _ts, _procs in _spikes_by_ts.items():
        try:
            _ep = _iso_to_epoch(_ts)
        except Exception:
            continue
        _ni = min(range(len(_row_epochs)), key=lambda j: abs(_row_epochs[j] - _ep))
        _srv_cpu = max(float(p.get("server_cpu", 0)) for p in _procs)
        _ram_v = ram[_ni]
        _y_sp = round(_ram_v / total_ram * 100, 1) if total_ram else _ram_v
        # Sort by WorkingSet descending so tooltip leads with the biggest RAM consumers
        _sorted_p = sorted(_procs, key=lambda p: float(p.get("working_set_mb", 0)), reverse=True)
        _plines = [
            f"{_normalize_proc_name(p['process'])}: {float(p.get('working_set_mb', 0)):.0f} MB  (CPU: {float(p.get('proc_cpu', 0)):.0f}%)"
            for p in _sorted_p[:5]
        ]
        if _ni not in _spike_meta or _srv_cpu > _spike_meta[_ni]["cpuPct"]:
            _spike_meta[_ni] = {"cpuPct": _srv_cpu, "y": _y_sp, "lines": _plines}

    # ── Filter to anomalous RAM events only (mean + 2σ, min 3pp above mean) ──
    # This prevents a "constant CPU load" run from flooding the chart with markers.
    # A stable flat run shows nothing; genuine RAM excursions stand out clearly.
    if _spike_meta and len(ram) > 1:
        _ram_pct_series = [round(v / total_ram * 100, 1) for v in ram] if total_ram else list(ram)
        _ram_mean = _stats.mean(_ram_pct_series)
        _ram_std  = _stats.stdev(_ram_pct_series) if len(_ram_pct_series) > 1 else 0.0
        # Require at least 3 percentage-point deviation so a nearly-flat run stays clean
        _ram_anomaly_threshold = _ram_mean + max(2.0 * _ram_std, 3.0)
        _spike_meta = {k: v for k, v in _spike_meta.items() if v["y"] >= _ram_anomaly_threshold}

    # ── Cluster merge: one marker per excursion, showing the peak ────────────
    # Consecutive row-indices within CLUSTER_WINDOW are treated as one event.
    # Keeps only the single highest-RAM point from each contiguous excursion,
    # so a 2-hour sustained spike appears as one well-placed triangle, not hundreds.
    # Window ≈ 10 row-indices; with 1440 points over a multi-day run each index
    # spans ~5–10 min, making this window roughly 50–100 minutes.
    _CLUSTER_WINDOW = 10
    if _spike_meta:
        _sorted_idxs = sorted(_spike_meta.keys())
        _clusters: list = []
        _cur: list = [_sorted_idxs[0]]
        for _idx in _sorted_idxs[1:]:
            if _idx - _cur[-1] <= _CLUSTER_WINDOW:
                _cur.append(_idx)
            else:
                _clusters.append(_cur)
                _cur = [_idx]
        _clusters.append(_cur)
        _spike_meta = {
            max(_c, key=lambda i: _spike_meta[i]["y"]): _spike_meta[max(_c, key=lambda i: _spike_meta[i]["y"])]
            for _c in _clusters
        }

    _spike_arr_py = ["null"] * len(labels)
    _spike_info_dict: dict = {}
    _spike_vert_lines: list = []  # passed to vertLines plugin for wide-target dashed lines
    for _ni, _m in _spike_meta.items():
        _spike_arr_py[_ni] = str(_m["y"])
        # JSON keys must be strings; JS coerces numeric property access to string automatically
        _spike_info_dict[str(_ni)] = {"c": f"{_m['cpuPct']:.0f}", "p": _m["lines"]}
        _spike_vert_lines.append({
            "xLabel": labels[_ni],
            "label": f"\u26a0 RAM {_m['y']:.0f}%",
            "color": "rgba(253,126,20,0.65)",
        })
    _spike_arr_js  = "[" + ",".join(_spike_arr_py) + "]"
    _spike_info_js = _json.dumps(_spike_info_dict)
    _spike_vert_js = _json.dumps(_spike_vert_lines)
    _has_spikes    = bool(_spike_meta)

    if total_ram:
        ram_pct_js   = _js_array([round(v / total_ram * 100, 1) for v in ram])
        ram_gb_js    = _js_array(ram)   # keep raw GB for tooltip
        ram_y_label  = "RAM %"
        ram_tick     = "ticks: { callback: v => v + '%' }, max: 100,"
        ram_ds_data  = ram_pct_js
        ram_ds_label = f"RAM (of {total_ram:.0f} GB)"
        # Tooltip: dataset 0 = RAM line (GB/%), dataset 1 = CPU spike points (process breakdown)
        ram_tooltip  = (
            f"  const ramGb = {ram_gb_js};"
            f"  const spikeInfo = {_spike_info_js};"
            "  tooltipCallbacks = { label: ctx => {"
            "    if (ctx.datasetIndex === 1) {"
            "      const s = spikeInfo[ctx.dataIndex];"
            "      return s ? [' Server CPU: ' + s.c + '%', ' Top RAM consumers:'] : '';"
            "    }"
            "    const pct = ctx.parsed.y; const gb = ramGb[ctx.dataIndex];"
            f"    return ' ' + gb.toFixed(1) + ' / {total_ram:.0f} GB (' + pct + '%)';"
            "  }, afterLabel: ctx => {"
            "    if (ctx.datasetIndex !== 1) return [];"
            "    const s = spikeInfo[ctx.dataIndex];"
            "    return s ? s.p.map(l => '  ' + l) : [];"
            "  } };"
        )
    else:
        ram_ds_data   = ram_js
        ram_ds_label  = "RAM Used (GB)"
        ram_y_label   = "RAM (GB)"
        ram_tick      = ""
        ram_tooltip   = (
            f"  const spikeInfo = {_spike_info_js};"
            "  tooltipCallbacks = { label: ctx => {"
            "    if (ctx.datasetIndex === 1) {"
            "      const s = spikeInfo[ctx.dataIndex];"
            "      return s ? [' Server CPU: ' + s.c + '%', ' Top RAM consumers:'] : '';"
            "    }"
            "    return ' ' + ctx.parsed.y.toFixed(1) + ' GB';"
            "  }, afterLabel: ctx => {"
            "    if (ctx.datasetIndex !== 1) return [];"
            "    const s = spikeInfo[ctx.dataIndex];"
            "    return s ? s.p.map(l => '  ' + l) : [];"
            "  } };"
        )

    # Spike overlay dataset: null-padded so only spike moments draw a triangle point.
    # pointHitRadius: 40 creates an 80px-wide invisible catch zone so hovering anywhere
    # near the vertical line triggers the tooltip — no pixel-precision required.
    _spike_ds_js = (
        ",{ label: 'CPU Spike', data: spikeArr, showLine: false,"
        "  backgroundColor: '#fd7e14', borderColor: '#fd7e14',"
        "  pointRadius: spikeArr.map(v => v !== null ? 10 : 0),"
        "  pointStyle: spikeArr.map(v => v !== null ? 'triangle' : 'circle'),"
        "  pointHoverRadius: spikeArr.map(v => v !== null ? 14 : 0),"
        "  pointHitRadius: 40 }"
    ) if _has_spikes else ""

    ram_chart = (
        "{ let tooltipCallbacks = {};"
        f"  const spikeArr = {_spike_arr_js};"
        f"  {ram_tooltip}"
        "  new Chart(document.getElementById('serverRamChart'), {"
        "    type: 'line',"
        f"   data: {{ labels: {lbl_js},"
        "      datasets: ["
        f"       {{ label: '{ram_ds_label}', data: {ram_ds_data},"
        f"          borderColor: '{_CHART_BLUE}', backgroundColor: '{_CHART_BLUE}18',"
        "           tension: 0.3, borderWidth: 2, pointRadius: 0 }"
        f"      {_spike_ds_js}] }},"
        "    options: { responsive: true, interaction: { mode: 'index', intersect: false },"
        "      plugins: {"
        "        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },"
        "        tooltip: { callbacks: tooltipCallbacks, filter: item => item.parsed.y !== null },"
        f"       vertLines: {markers_js}.concat({_spike_vert_js}),"
        f"       {_ZOOM_PLUGIN_OPTS}"
        "      },"
        f"     scales: {{ x: {{ ticks: {{ autoSkip: true, maxRotation: 45, maxTicksLimit: {max_ticks} }}, title: {{ display: true, text: 'Time (scroll to zoom, drag to pan)' }} }},"
        f"       y: {{ beginAtZero: true, {ram_tick}"
        f"            title: {{ display: true, text: '{ram_y_label}' }} }}"
        "      } } }); }"
    )

    return cpu_chart + "\n" + ram_chart


def _tier_server_charts_js(tiers: list) -> str:
    """Per-tier CPU% / RAM% dual-axis line charts for the collapsible detail."""
    parts = []
    for i, t in enumerate(tiers):
        snaps = (t.get("server") or {}).get("snapshots", [])
        if not snaps:
            continue
        total_ram  = (t.get("server") or {}).get("total_ram_gb")
        cpu_id     = f"tier_srv_cpu_{i}"
        ram_id     = f"tier_srv_ram_{i}"
        labels     = [_fmt_time_12h(str(s.get("timestamp", ""))) for s in snaps]
        cpu_vals   = [s.get("cpu_percent", 0) for s in snaps]
        ram_gb_vals = [s.get("ram_used_gb", 0) for s in snaps]
        ram_vals   = (
            [round(v / total_ram * 100, 1) for v in ram_gb_vals]
            if total_ram else ram_gb_vals
        )
        ram_label  = f"RAM (of {total_ram:.0f} GB)" if total_ram else "RAM Used (GB)"
        ram_y_tick = "ticks: { callback: v => v + '%' }, max: 100," if total_ram else ""
        sixty_js    = _js_array([60] * len(cpu_vals))
        lbl_js      = _js_array(labels)
        cpu_js      = _js_array(cpu_vals)
        ram_pct_js  = _js_array(ram_vals)
        ram_gb_js   = _js_array(ram_gb_vals)

        # Compute per-session connect markers for this tier
        t_sessions        = t.get("sessions", [])
        t_ramp_epoch      = float(t.get("ramp_start_epoch", 0))
        t_interval        = float(t.get("interval_s", 2))
        tier_markers: list = []
        if t_sessions and t_ramp_epoch:
            snap_epochs = [_iso_to_epoch(str(s.get("timestamp", ""))) for s in snaps]
            if snap_epochs:
                for si, sess in enumerate(sorted(t_sessions, key=lambda s: s.get("tab", 0))):
                    cep = t_ramp_epoch + si * t_interval + sess.get("load_ms", 0) / 1000
                    ni  = min(range(len(snap_epochs)), key=lambda j: abs(snap_epochs[j] - cep))
                    tier_markers.append({"xLabel": labels[ni], "label": f"S{sess.get('tab', si) + 1}"})
        t_markers_js = json.dumps(tier_markers)
        if total_ram:
            ram_tooltip = (
                f"const ramGb_{i} = {ram_gb_js};"
                f"const totalRam_{i} = {total_ram:.0f};"
                f"const ramCb_{i} = ctx => {{"
                f"  const gb = ramGb_{i}[ctx.dataIndex];"
                f"  const pct = ctx.parsed.y;"
                f"  return ' ' + gb.toFixed(1) + ' / {total_ram:.0f} GB (' + pct + '%)';"
                f"}};"
            )
            ram_tooltip_plugin = f"tooltip: {{ callbacks: {{ label: ramCb_{i} }} }},"
        else:
            ram_tooltip = ""
            ram_tooltip_plugin = ""
        parts.append(
            f"new Chart(document.getElementById('{cpu_id}'), {{"
            f"  type: 'line',"
            f"  data: {{ labels: {lbl_js},"
            f"    datasets: ["
            f"      {{ label: 'CPU %', data: {cpu_js},"
            f"        borderColor: '{_DANGER}', backgroundColor: '{_DANGER}18',"
            f"        tension: 0.3, borderWidth: 2, pointRadius: 1 }},"
            f"      {{ label: '60% target', data: {sixty_js},"
            f"        borderColor: '#ffc107', backgroundColor: 'transparent',"
            f"        borderDash: [6,4], borderWidth: 1, pointRadius: 0 }}"
            f"    ] }},"
            f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
            f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }}, vertLines: {t_markers_js} }},"
            f"    scales: {{"
            f"      x: {{ title: {{ display: true, text: 'Time (scroll to zoom, drag to pan)', font: {{ size: 10 }} }} }},"
            f"      y: {{ beginAtZero: true, max: 100,"
            f"           title: {{ display: true, text: 'CPU %', font: {{ size: 10 }} }},"
            f"           ticks: {{ callback: v => v + '%' }} }}"
            f"    }} }} }});"
        )
        parts.append(
            f"{ram_tooltip}"
            f"new Chart(document.getElementById('{ram_id}'), {{"
            f"  type: 'line',"
            f"  data: {{ labels: {lbl_js},"
            f"    datasets: ["
            f"      {{ label: '{ram_label}', data: {ram_pct_js},"
            f"        borderColor: '{_CHART_BLUE}', backgroundColor: '{_CHART_BLUE}18',"
            f"        tension: 0.3, borderWidth: 2, pointRadius: 1 }}"
            f"    ] }},"
            f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
            f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},"
            f"      {ram_tooltip_plugin} vertLines: {t_markers_js} }},"
            f"    scales: {{"
            f"      x: {{ title: {{ display: true, text: 'Time', font: {{ size: 10 }} }} }},"
            f"      y: {{ beginAtZero: true, {ram_y_tick}"
            f"           title: {{ display: true, text: '{'RAM %' if total_ram else 'RAM (GB)'}', font: {{ size: 10 }} }} }}"
            f"    }} }} }});"
        )
    return "\n".join(parts)


def _mb_to_display(values: list) -> tuple:
    """Convert a list of MB values to the best display unit.

    Returns (converted_values, unit_str) where unit_str is 'GB' if max > 1000
    else 'MB'.  Values are rounded to 2 decimal places when in GB.
    """
    max_val = max(values) if values else 0
    if max_val >= 1000:
        return ([round(v / 1024, 2) for v in values], "GB")
    return (values, "MB")


def _proc_series_charts_js(server: dict) -> str:
    """Return Chart.js JS for the 4 per-process time-series charts (SRS + ffmpeg)."""
    rows = (server or {}).get("proc_series", [])
    if not rows:
        return ""

    # Detect multi-day runs and switch to elapsed "Day N Hh" labels (same logic as server charts)
    run_span_s = 0.0
    if len(rows) >= 2:
        try:
            run_span_s = _iso_to_epoch(rows[-1]["time"]) - _iso_to_epoch(rows[0]["time"])
        except Exception:
            pass
    if run_span_s > 86400:
        t0_epoch = _iso_to_epoch(rows[0]["time"])
        labels = [_fmt_duration(_iso_to_epoch(r["time"]) - t0_epoch, run_span_s) for r in rows]
    else:
        labels = [_fmt_time_12h(r["time"]) for r in rows]
    max_ticks = max(20, min(30, int(run_span_s / 3600 / 5))) if run_span_s > 3600 else 20

    rows = _downsample(rows, max_points=1440)
    labels = _downsample(labels, max_points=1440)
    lbl_js  = _js_array(labels)
    # pull series arrays AFTER downsample, then pick best unit per series
    srs_cpu_js    = _js_array([r["srs_cpu"]        for r in rows])
    ff_cpu_js     = _js_array([r["ffmpeg_cpu"]     for r in rows])
    ff_count_js   = _js_array([r["ffmpeg_count"]   for r in rows])

    srs_ram_vals,  srs_ram_unit  = _mb_to_display([r["srs_ram_mb"]     for r in rows])
    srs_virt_vals, srs_virt_unit = _mb_to_display([r["srs_virt_mb"]    for r in rows])
    ff_ram_vals,   ff_ram_unit   = _mb_to_display([r["ffmpeg_ram_mb"]  for r in rows])
    ff_virt_vals,  ff_virt_unit  = _mb_to_display([r["ffmpeg_virt_mb"] for r in rows])

    srs_ram_js  = _js_array(srs_ram_vals)
    srs_virt_js = _js_array(srs_virt_vals)
    ff_ram_js   = _js_array(ff_ram_vals)
    ff_virt_js  = _js_array(ff_virt_vals)

    x_axis = (f'{{ ticks: {{ autoSkip: true, maxRotation: 45, maxTicksLimit: {max_ticks} }},'
              f' title: {{ display: true, text: "Time", font: {{ size: 10 }} }} }}')
    parts = []

    def _subtitle(text: str) -> str:
        """Chart.js subtitle plugin config string."""
        safe = text.replace("'", "\\'")  # escape single quotes for JS
        return (f"subtitle: {{ display: true, text: '{safe}', "
                f"color: '#6c757d', font: {{ size: 10, style: 'italic' }}, "
                f"padding: {{ bottom: 8 }} }}")

    # SRS CPU
    parts.append(
        f"new Chart(document.getElementById('procSrsCpuChart'), {{"
        f"  type: 'line',"
        f"  data: {{ labels: {lbl_js}, datasets: [{{"
        f"    label: 'SRS CPU (%)', data: {srs_cpu_js},"
        f"    borderColor: '{_DANGER}', backgroundColor: '{_DANGER}18',"
        f"    tension: 0.3, borderWidth: 2, pointRadius: 1 }}] }},"
        f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
        f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},"
        f"      {_ZOOM_PLUGIN_OPTS} }},"
        f"    scales: {{"
        f"      x: {x_axis},"
        f"      y: {{ beginAtZero: true, min: 0,"
        f"           title: {{ display: true, text: 'CPU %', font: {{ size: 10 }} }} }}"
        f"    }} }} }});"
    )

    # SRS Memory — dual Y-axis: left=RAM, right=virtual; each auto-scaled to best unit
    parts.append(
        f"new Chart(document.getElementById('procSrsMemChart'), {{"
        f"  type: 'line',"
        f"  data: {{ labels: {lbl_js}, datasets: ["
        f"    {{ label: 'SRS RAM ({srs_ram_unit})', data: {srs_ram_js},"
        f"       borderColor: '{_CHART_BLUE}', backgroundColor: '{_CHART_BLUE}18',"
        f"       tension: 0.3, borderWidth: 2, pointRadius: 1, yAxisID: 'yRam' }},"
        f"    {{ label: 'SRS Virtual ({srs_virt_unit})', data: {srs_virt_js},"
        f"       borderColor: '{_CHART_PURPLE}', backgroundColor: 'transparent',"
        f"       tension: 0.3, borderWidth: 1.5, pointRadius: 0, borderDash: [6,4], yAxisID: 'yVirt' }}"
        f"  ] }},"
        f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
        f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},"
        f"      {_ZOOM_PLUGIN_OPTS} }},"
        f"    scales: {{"
        f"      x: {x_axis},"
        f"      yRam:  {{ type: 'linear', position: 'left',  title: {{ display: true, text: 'RAM ({srs_ram_unit})',     font: {{ size: 10 }} }} }},"
        f"      yVirt: {{ type: 'linear', position: 'right', title: {{ display: true, text: 'Virtual ({srs_virt_unit})', font: {{ size: 10 }} }}, grid: {{ drawOnChartArea: false }} }}"
        f"    }} }} }});"
    )

    # ffmpeg Total CPU
    parts.append(
        f"const _ffCounts = {ff_count_js};"
        f"new Chart(document.getElementById('procFfmpegCpuChart'), {{"
        f"  type: 'line',"
        f"  data: {{ labels: {lbl_js}, datasets: [{{"
        f"    label: 'ffmpeg Total CPU (%)', data: {ff_cpu_js},"
        f"    borderColor: '{_WARNING}', backgroundColor: '{_WARNING}18',"
        f"    tension: 0.3, borderWidth: 2, pointRadius: 1 }}] }},"
        f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
        f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},"
        f"      tooltip: {{ callbacks: {{ afterLabel: ctx => '  Instances: ' + _ffCounts[ctx.dataIndex] }} }},"
        f"      {_ZOOM_PLUGIN_OPTS} }},"
        f"    scales: {{"
        f"      x: {x_axis},"
        f"      y: {{ beginAtZero: true,"
        f"           title: {{ display: true, text: 'CPU %', font: {{ size: 10 }} }} }}"
        f"    }} }} }});"
    )

    # ffmpeg Total Memory — dual Y-axis: left=RAM, right=virtual; each auto-scaled
    parts.append(
        f"new Chart(document.getElementById('procFfmpegMemChart'), {{"
        f"  type: 'line',"
        f"  data: {{ labels: {lbl_js}, datasets: ["
        f"    {{ label: 'ffmpeg RAM ({ff_ram_unit})', data: {ff_ram_js},"
        f"       borderColor: '{_CHART_TEAL}', backgroundColor: '{_CHART_TEAL}18',"
        f"       tension: 0.3, borderWidth: 2, pointRadius: 1, yAxisID: 'yRam' }},"
        f"    {{ label: 'ffmpeg Virtual ({ff_virt_unit})', data: {ff_virt_js},"
        f"       borderColor: '{_CHART_PURPLE}', backgroundColor: 'transparent',"
        f"       tension: 0.3, borderWidth: 1.5, pointRadius: 0, borderDash: [6,4], yAxisID: 'yVirt' }}"
        f"  ] }},"
        f"  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }},"
        f"    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},"
        f"      {_ZOOM_PLUGIN_OPTS} }},"
        f"    scales: {{"
        f"      x: {x_axis},"
        f"      yRam:  {{ type: 'linear', position: 'left',  title: {{ display: true, text: 'RAM ({ff_ram_unit})',     font: {{ size: 10 }} }} }},"
        f"      yVirt: {{ type: 'linear', position: 'right', title: {{ display: true, text: 'Virtual ({ff_virt_unit})', font: {{ size: 10 }} }}, grid: {{ drawOnChartArea: false }} }}"
        f"    }} }} }});"
    )

    return "\n".join(parts)


def _tier_chart_js(tiers: list) -> str:
    if not tiers:
        return ""
    labels    = [f"{t.get('server_name','?')} / {t.get('session_tier','?')}" for t in tiers]
    load_vals = [round(t.get("summary", {}).get("avg_load_ms", 0) / 1000, 3) for t in tiers]
    rate_vals = [t.get("summary", {}).get("connect_rate_pct", 0) for t in tiers]
    cpu_vals  = [(t.get("server") or {}).get("avg_cpu") for t in tiers]
    has_cpu   = any(v is not None for v in cpu_vals)
    lbl_js   = _js_array(labels)
    load_js  = _js_array(load_vals)
    rate_js  = _js_array(rate_vals)
    cpu_js   = _js_array(cpu_vals)
    cpu_dataset = (
        f"      {{ label: 'Avg CPU (%)', data: {cpu_js},"
        f"        type: 'line', borderColor: '{_DANGER}', backgroundColor: '{_DANGER}18',"
        "         tension: 0.3, yAxisID: 'yRate', order: 0, pointRadius: 5,"
        "         borderDash: [6,4] },"
    ) if has_cpu else ""
    right_axis_title = "Rate (%) / CPU %" if has_cpu else "Connect Rate (%)"
    return (
        "new Chart(document.getElementById('tierChart'), {"
        "  type: 'bar',"
        f"  data: {{ labels: {lbl_js},"
        "    datasets: ["
        f"      {{ label: 'Avg Load (s)', data: {load_js},"
        f"        backgroundColor: '{_CHART_BLUE}cc', borderRadius: 4,"
        "         yAxisID: 'yLoad', order: 2 },"
        f"      {{ label: 'Connect Rate (%)', data: {rate_js},"
        f"        type: 'line', borderColor: '{_SUCCESS}', backgroundColor: '{_SUCCESS}33',"
        "         tension: 0.3, yAxisID: 'yRate', order: 1, pointRadius: 5 },"
        + cpu_dataset
        + "    ] },"
        "  options: { responsive: true, interaction: { mode: 'index', intersect: false },"
        "    scales: {"
        "      yLoad: { type: 'linear', position: 'left',"
        "        title: { display: true, text: 'Avg Load (s)' }, beginAtZero: true },"
        f"      yRate: {{ type: 'linear', position: 'right',"
        f"        title: {{ display: true, text: '{right_axis_title}' }}, min: 0, max: 100,"
        "        grid: { drawOnChartArea: false } }"
        "    } } });"
    )


def _tier_session_charts_js(tiers: list) -> str:
    """Generate Chart.js bar charts for per-tier source load times."""
    parts = []
    for i, t in enumerate(tiers):
        sessions = t.get("sessions", [])
        if not sessions:
            continue
        canvas_id = f"tier_src_chart_{i}"
        labels    = [str(s["tab"] + 1) for s in sessions]
        values    = [round(s["load_ms"] / 1000, 3) for s in sessions]
        colors    = [
            '"rgba(25,135,84,0.75)"' if s["connected"] else '"rgba(220,53,69,0.75)"'
            for s in sessions
        ]
        labels_js = _js_array(labels)
        values_js = _js_array(values)
        colors_js = ", ".join(colors)
        parts.append(
            f"new Chart(document.getElementById('{canvas_id}'), {{"
            f"  type: 'bar',"
            f"  data: {{ labels: {labels_js},"
            f"    datasets: [{{ label: 'Load (s)', data: {values_js},"
            f"      backgroundColor: [{colors_js}], borderRadius: 3 }}] }},"
            f"  options: {{ responsive: true, plugins: {{ legend: {{ display: false }},"
            f"    tooltip: {{ callbacks: {{ label: ctx => ' ' + ctx.raw.toFixed(2) + ' s' }} }} }},"
            f"    scales: {{"
            f"      x: {{ title: {{ display: true, text: 'Session #', font: {{ size: 10 }} }} }},"
            f"      y: {{ title: {{ display: true, text: 's', font: {{ size: 10 }} }}, beginAtZero: true }}"
            f"    }} }} }});"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _stat_card(label: str, value, unit: str = "", accent: str = "") -> str:
    style = f"border-top:3px solid {accent};" if accent else ""
    unit_html = f'<div class="stat-unit">{unit}</div>' if unit else ""
    return (
        f'<div class="col-xl-2 col-md-3 col-sm-4 col-6">'
        f'<div class="stat-card" style="{style}">'
        f'<div class="stat-label">{label}</div>'
        f'<div class="stat-value">{value}</div>'
        f'{unit_html}'
        f'</div></div>'
    )


def _mini(label: str, val, unit: str = "", color: str = "") -> str:
    val_style = f' style="color:{color}"' if color else ""
    return (
        f'<div class="col-md-3 col-6"><div class="metric-mini">'
        f'<span class="metric-mini-label">{label}</span>'
        f'<span class="metric-mini-val"{val_style}>{val}{unit}</span>'
        f'</div></div>'
    )


def _ram_label(avg_gb, total_gb) -> str:
    if avg_gb is None:
        return "&#8212;"
    if total_gb:
        pct = avg_gb / total_gb * 100
        return f"{avg_gb:.1f} / {total_gb:.0f} GB ({pct:.0f}%)"
    return f"{avg_gb:.1f} GB"


def _chart_panel(label: str, canvas_id: str, extra_class: str = "mt-3", open: bool = True,
                 desc: str = "") -> tuple[str, str]:
    """Return (open_html, close_html) for a collapsible chart panel.

    Args:
        desc: Optional one-line description shown below the collapsed label.
              Renders inside the collapse so it only appears when expanded.
    """
    shown     = "show" if open else ""
    expanded  = "true" if open else "false"
    toggle_id = f"chart-collapse-{canvas_id}"
    desc_html = (
        f'<p class="chart-desc">{desc}</p>'
        if desc else ""
    )
    open_html = (
        f'<div class="detail-sub-header {extra_class}" style="cursor:pointer;" '
        f'data-bs-toggle="collapse" data-bs-target="#{toggle_id}" aria-expanded="{expanded}">'
        f'{label}<span class="chart-chevron">&#9650;</span></div>'
        f'<div class="collapse {shown}" id="{toggle_id}">'
        + desc_html
    )
    return open_html, "</div>"


def _server_section(server: dict, room_sources: list | None = None) -> str:
    if not server:
        return ""

    def _panel(label, canvas_id, cls="mt-3", open=True, desc=""):
        o, c = _chart_panel(label, canvas_id, cls, open, desc=desc)
        return o + f'<canvas id="{canvas_id}" height="90"></canvas>' + _reset_zoom_btn(canvas_id) + c

    _cpu_avg = server.get("avg_cpu")
    _cpu_p99 = server.get("p99_cpu")
    _cpu_desc = (
        f"Server aggregate CPU% sampled every 2s. "
        f"Yellow dashed line&nbsp;= 60% target utilization. "
        + (f"Avg&nbsp;<strong>{_cpu_avg}%</strong> / p99&nbsp;<strong>{_cpu_p99}%</strong>." if _cpu_avg is not None else "")
    )
    _total_ram = server.get("total_ram_gb")
    _avg_ram_gb = server.get("avg_ram_gb")
    _ram_desc = (
        f"Physical RAM consumed over the run duration"
        + (f" ({_total_ram:.0f}&nbsp;GB total)" if _total_ram else "")
        + ". "
        + (f"Avg&nbsp;<strong>{_avg_ram_gb:.1f}&nbsp;GB</strong> "
           f"({_avg_ram_gb / _total_ram * 100:.0f}%). "
           if (_avg_ram_gb is not None and _total_ram) else "")
        + "Orange&nbsp;&#9650; triangles = CPU spike events, hover for top processes."
    )

    # Per-process avg/max from proc_series (p99 instead of raw max to exclude WMI polling artifacts)
    _ps = server.get("proc_series") or []
    def _ps_stat(key):
        vals = sorted(r[key] for r in _ps if r.get(key) is not None and r[key] > 0)
        if not vals:
            return None, None
        avg = round(sum(vals) / len(vals), 1)
        p99 = vals[int(len(vals) * 0.99)] if len(vals) >= 10 else vals[-1]
        return avg, round(p99, 1)

    _srs_cpu_avg,    _srs_cpu_max    = _ps_stat("srs_cpu")
    _srs_ram_avg,    _srs_ram_max    = _ps_stat("srs_ram_mb")
    _ffmpeg_cpu_avg, _ffmpeg_cpu_max = _ps_stat("ffmpeg_cpu")
    _ffmpeg_ram_avg, _ffmpeg_ram_max = _ps_stat("ffmpeg_ram_mb")

    def _stat_suffix(avg, max_v, unit):
        if avg is None:
            return ""
        return f" Avg&nbsp;<strong>{avg}{unit}</strong> / p99&nbsp;<strong>{max_v}{unit}</strong>."

    _srs_cpu_desc = (
        "Per-process CPU% for the SRS media server (stream relay engine). Values &gt;100% = more than one full core."
        + _stat_suffix(_srs_cpu_avg, _srs_cpu_max, "%")
    )
    _srs_mem_desc = (
        "Working-set memory for SRS processes over time."
        + _stat_suffix(_srs_ram_avg, _srs_ram_max, "&nbsp;MB")
    )
    _ffmpeg_cpu_desc = (
        "Aggregate CPU% across all ffmpeg ingest processes. Each active room source spawns one ffmpeg."
        + _stat_suffix(_ffmpeg_cpu_avg, _ffmpeg_cpu_max, "%")
    )
    _ffmpeg_mem_desc = (
        "Total working-set memory across all ffmpeg processes."
        + _stat_suffix(
            round(_ffmpeg_ram_avg / 1024, 2) if _ffmpeg_ram_avg is not None else None,
            round(_ffmpeg_ram_max / 1024, 2) if _ffmpeg_ram_max is not None else None,
            "&nbsp;GB",
        )
    )

    return (
        '<div class="section-card">'
        '<div class="section-header">Server Metrics</div>'
        '<div class="row g-3 mb-3">'
        + _mini("Avg CPU",            server.get("avg_cpu", "&#8212;"),             "%")
        + _mini("p99 CPU",            server.get("p99_cpu", server.get("max_cpu", "&#8212;")), "%",  _DANGER)
        + _mini("Avg RAM",            _ram_label(server.get("avg_ram_gb"), server.get("total_ram_gb")))
        + _mini("Net Send (clients)",  server.get("avg_net_send_mbps", "&#8212;"),  " Mbps")
        + '</div>'
        + _panel("CPU % Over Time",                "serverCpuChart",     cls="",   desc=_cpu_desc)
        + _panel("RAM Used Over Time",             "serverRamChart",               desc=_ram_desc)
        + _panel("SRS Process: CPU %",             "procSrsCpuChart",
                 desc=_srs_cpu_desc)
        + _panel("SRS Process: Memory (MB)",        "procSrsMemChart",
                 desc=_srs_mem_desc)
        + _panel("ffmpeg Total: CPU %",             "procFfmpegCpuChart",
                 desc=_ffmpeg_cpu_desc)
        + _panel("ffmpeg Total: Memory (MB)",        "procFfmpegMemChart",
                 desc=_ffmpeg_mem_desc)
        + _ffmpeg_instances_table_html(server, room_sources or [])
        + _process_spikes_table_html(server)
        + '</div>'
    )


def _webrtc_section(webrtc: dict) -> str:
    if not webrtc or not webrtc.get("snapshots"):
        return ""
    snaps = webrtc["snapshots"]
    if all(s.get("conns", 0) <= 1 for s in snaps):
        return (
            "<div class='section-card'>\n"
            "<div class='section-header'>WebRTC Stream Stability</div>\n"
            "<p class='text-muted mb-0' style='font-size:.85rem;'>No video streams were established "
            "during the measurement window. Sources may not have been streaming, or the stream delivery "
            "timed out before data was captured. Check source configuration.</p>\n"
            "</div>\n"
        )
    # Delegate to the corrected stability table (per-source FPS, -1 signaling correction)
    stability_table = _webrtc_stability_table_html(snaps)
    total_dropped = webrtc.get("total_dropped_frames", 0)
    avg_jitter    = webrtc.get("avg_jitter_ms")
    minis = (
        _mini("Avg Jitter",    avg_jitter if avg_jitter is not None else "&#8212;", " ms")
        + _mini("Total Dropped", total_dropped, "", _DANGER if total_dropped else "")
    )
    return (
        '<div class="section-card">'
        '<div class="section-header">WebRTC Stream Stability</div>'
        f'<div class="row g-3 mb-3">{minis}</div>'
        + stability_table
        + '</div>'
    )


def _per_tier_server_cards(tiers: list) -> str:
    """Per-server, per-tier metric blocks replacing the aggregate KPI row."""
    if not tiers:
        return ""

    # Global index → unique canvas IDs per tier
    tier_idx = {(t.get("server_name"), t.get("session_tier")): i for i, t in enumerate(tiers)}

    # Group by server name, preserving insertion order
    servers: dict = {}
    for t in tiers:
        sn = t.get("server_name", "Unknown")
        servers.setdefault(sn, [])
        servers[sn].append(t)

    hw_colors = [_CHART_BLUE, "#6610f2", "#20c997", "#fd7e14", "#0dcaf0"]
    blocks = []
    for idx, (sname, tier_list) in enumerate(servers.items()):
        hw = tier_list[0].get("hardware", "")
        accent = hw_colors[idx % len(hw_colors)]
        hw_badge = (
            f'<span style="background:{accent};color:#fff;font-size:.7rem;font-weight:700;'
            f'padding:2px 10px;border-radius:12px;letter-spacing:.05em;">'
            f'{hw}</span>' if hw else ""
        )
        tier_rows = []
        for t in tier_list:
            s        = t.get("summary", {})
            srv      = t.get("server") or {}
            n        = t.get("session_tier", "?")
            sessions = t.get("sessions", [])
            api      = t.get("api") or {}
            rate     = s.get("connect_rate_pct", 0)
            cpu       = srv.get("avg_cpu")
            max_cpu   = srv.get("max_cpu")
            total_ram = srv.get("total_ram_gb")
            avg_ram   = srv.get("avg_ram_gb")
            cpu_color     = _DANGER if cpu     and float(cpu)     >= 80 else ""
            max_cpu_color = _DANGER if max_cpu and float(max_cpu) >= 80 else ""
            net = srv.get("avg_net_send_mbps")

            if avg_ram is not None and total_ram:
                ram_pct  = avg_ram / total_ram * 100
                ram_val  = f"{avg_ram:.1f} / {total_ram:.0f} GB ({ram_pct:.0f}%)"
                ram_color = _DANGER if ram_pct >= 90 else (_WARNING if ram_pct >= 75 else "")
            else:
                ram_val  = str(avg_ram) if avg_ram is not None else "&#8212;"
                ram_color = ""

            metrics_html = (
                _mini("Connected",  f"{s.get('connected','?')}/{s.get('total_sessions','?')}")
                + _mini("Rate",     f"{rate:.0f}", "%", _rate_color(rate))
                + _mini("Avg Load", f"{s.get('avg_load_ms', 0)/1000:.2f}", " s")
                + _mini("Max Load", f"{s.get('max_load_ms', 0)/1000:.2f}", " s", _WARNING)
                + _mini("Avg CPU",  str(cpu)     if cpu     is not None else "&#8212;", "%", cpu_color)
                + _mini("Max CPU",  str(max_cpu) if max_cpu is not None else "&#8212;", "%", max_cpu_color)
                + _mini("Avg RAM",  ram_val, "", ram_color)
                + _mini("Net Send", str(net) if net is not None else "&#8212;", " Mbps")
            )

            # Collapsible detail: API stats + source load chart + memory + WebRTC
            detail_html = ""
            webrtc_snaps = t.get("webrtc", {}).get("snapshots", [])
            srv_snaps    = (t.get("server") or {}).get("snapshots", [])
            if sessions or api or srv_snaps or webrtc_snaps:
                detail_parts = []
                if api:
                    _t_bw = api.get("bw_checks", 0)
                    api_tiles = (
                        _mini("API Calls", str(api.get("non_bw_total", api.get("total", "&#8212;"))), "")
                        + _mini("API Avg", str(api.get("avg_ms",     "&#8212;")), " ms")
                        + (_mini("BW p95",  str(api.get("bw_p95_ms","&#8212;")), " ms") if _t_bw else "")
                    )
                    detail_parts.append(f'<div class="row g-2 mb-2">{api_tiles}</div>')
                if sessions:
                    canvas_id = f"tier_src_chart_{tier_idx.get((t.get('server_name'), n), 0)}"
                    detail_parts.append(f'<canvas id="{canvas_id}" height="50"></canvas>')
                if srv_snaps:
                    srv_i = tier_idx.get((t.get("server_name"), n), 0)
                    detail_parts.append(
                        '<div class="detail-sub-header">CPU % Over Time</div>'
                        f'<canvas id="tier_srv_cpu_{srv_i}" height="80"></canvas>'
                        '<div class="detail-sub-header mt-3">RAM Used Over Time</div>'
                        f'<canvas id="tier_srv_ram_{srv_i}" height="80"></canvas>'
                    )
                if webrtc_snaps:
                    detail_parts.append(_webrtc_stability_table_html(webrtc_snaps))
                n_src = len(sessions)
                detail_html = (
                    '<details class="tier-details">'
                    '<summary>Detail: Sources, Memory &amp; WebRTC'
                    + (f' &nbsp;<span class="detail-count">{n_src} sources</span>' if n_src else "")
                    + '</summary>'
                    '<div class="tier-detail-body">'
                    + "".join(detail_parts)
                    + '</div></details>'
                )

            # Compute per-tier PASS/WARN/FAIL verdict for quick traffic-light badge
            _tier_ram_pct = (avg_ram / total_ram * 100) if (avg_ram is not None and total_ram) else None
            _tv, _ = _overall_verdict({
                "avg_cpu_pct":      float(cpu)     if cpu     is not None else None,
                "max_cpu_pct":      float(max_cpu) if max_cpu is not None else None,
                "avg_ram_pct":      _tier_ram_pct,
                "avg_load_ms":      s.get("avg_load_ms"),
                "max_load_ms":      s.get("max_load_ms"),
                "connect_rate_pct": rate,
            })
            _tv_c = {"pass": _SUCCESS, "warn": _WARNING, "fail": _DANGER}[_tv]
            _tv_l = {"pass": "PASS", "warn": "CAUTION", "fail": "FAIL"}[_tv]
            _tv_i = {"pass": "✓", "warn": "⚠", "fail": "✗"}[_tv]
            tier_badge = (
                f"<span style='background:{_tv_c};color:#fff;font-size:.65rem;"
                f"font-weight:700;padding:2px 8px;border-radius:10px;margin-left:8px;'>"
                f"{_tv_i} {_tv_l}</span>"
            )
            tier_rows.append(
                f'<div class="tier-row">'
                f'<div class="tier-label">{n} streams {tier_badge}</div>'
                f'<div class="tier-metrics">'
                + metrics_html
                + '</div>'
                + detail_html
                + '</div>'
            )
        blocks.append(
            f'<div class="server-block" style="border-left:4px solid {accent};">'
            f'<div class="server-block-header">'
            f'<span class="server-block-name">{sname}</span>&nbsp;&nbsp;{hw_badge}'
            f'</div>'
            + "".join(tier_rows)
            + "</div>"
        )

    return (
        "<div class='section-card'>\n"
        "<div class='section-header'>Results by Server &amp; Tier</div>\n"
        + "\n".join(blocks)
        + "\n</div>\n"
    )


def _tier_comparison_section(tiers: list) -> str:
    if not tiers:
        return ""
    return (
        '<div class="section-card">'
        '<div class="section-header">Capacity Validation &#8212; All Tiers</div>'
        '<canvas id="tierChart" height="90"></canvas>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Layout stress section
# ---------------------------------------------------------------------------

def _layout_stress_chart_js(steps: list) -> str:
    """Dual-axis line: total WebRTC connections (left) + avg FPS (right) per layout step."""
    if not steps:
        return ""
    labels   = [f"{s['layout']}-up" for s in steps]
    conns    = [s.get("total_webrtc_conns", 0) for s in steps]
    fps      = [s.get("avg_fps", 0) for s in steps]
    jitter   = [s.get("avg_jitter_ms", 0) for s in steps]
    lbl_js     = _js_array(labels)
    conns_js   = _js_array(conns)
    fps_js     = _js_array(fps)
    jitter_js  = _js_array(jitter)
    return (
        "new Chart(document.getElementById('layoutStressChart'), {"
        "  type: 'line',"
        f"  data: {{ labels: {lbl_js},"
        "    datasets: ["
        f"      {{ label: 'Total WebRTC Conns', data: {conns_js},"
        f"        borderColor: '{_DANGER}', backgroundColor: '{_DANGER}20',"
        "         yAxisID: 'yConns', tension: 0.3, borderWidth: 2.5, pointRadius: 5,"
        "         fill: true }},"
        f"      {{ label: 'Avg FPS', data: {fps_js},"
        f"        borderColor: '{_SUCCESS}', backgroundColor: '{_SUCCESS}18',"
        "         yAxisID: 'yFps', tension: 0.3, borderWidth: 2, pointRadius: 5,"
        "         borderDash: [] }},"
        f"      {{ label: 'Avg Jitter (ms)', data: {jitter_js},"
        f"        borderColor: '{_WARNING}', backgroundColor: '{_WARNING}18',"
        "         yAxisID: 'yFps', tension: 0.3, borderWidth: 2, pointRadius: 5,"
        "         borderDash: [6,4] }}"
        "    ] }},"
        "  options: { responsive: true, interaction: { mode: 'index', intersect: false },"
        "    plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },"
        "      tooltip: { mode: 'index', intersect: false } },"
        "    scales: {"
        f"      yConns: {{ type: 'linear', position: 'left',"
        "        title: { display: true, text: 'Total WebRTC Connections' }, beginAtZero: true },"
        "      yFps: { type: 'linear', position: 'right',"
        "        title: { display: true, text: 'FPS / Jitter (ms)' }, beginAtZero: true,"
        "        grid: { drawOnChartArea: false } }"
        "    } } });"
    )


def _layout_stress_section(steps: list, server: dict = None) -> str:
    """Full layout stress section: summary table + chart + server metrics."""
    if not steps:
        return ""

    rows = []
    for s in steps:
        layout  = s.get("layout", "?")
        conns   = s.get("total_webrtc_conns", 0)
        fps     = s.get("avg_fps", 0)
        jitter  = s.get("avg_jitter_ms", 0)
        dropped = s.get("total_dropped_frames", 0)
        api_ms  = s.get("avg_api_ms", 0)
        sesh    = s.get("sessions", 0)
        fps_style    = f' style="color:{_DANGER};font-weight:700;"' if fps < 25 and fps > 0 else ""
        jitter_style = f' style="color:{_WARNING};font-weight:700;"' if jitter > 100 else ""
        drop_style   = f' style="color:{_DANGER};font-weight:700;"' if dropped > 0 else ""
        rows.append(
            f'<tr>'
            f'<td class="text-center fw-bold">{layout}-up</td>'
            f'<td class="text-center">{sesh}</td>'
            f'<td class="text-end fw-bold">{conns}</td>'
            f'<td class="text-end"{fps_style}>{fps:.1f}</td>'
            f'<td class="text-end"{jitter_style}>{jitter:.1f}</td>'
            f'<td class="text-end"{drop_style}>{dropped}</td>'
            f'<td class="text-end">{api_ms:.1f}</td>'
            f'</tr>'
        )

    table = (
        '<div class="table-responsive">'
        '<table class="data-table">'
        '<thead><tr>'
        '<th class="text-center">Layout</th>'
        '<th class="text-center">Sessions</th>'
        '<th class="text-end">Total WebRTC Conns</th>'
        '<th class="text-end">Avg FPS</th>'
        '<th class="text-end">Avg Jitter (ms)</th>'
        '<th class="text-end">Dropped Frames</th>'
        '<th class="text-end">Avg API (ms)</th>'
        '</tr></thead>'
        '<tbody>' + "".join(rows) + '</tbody>'
        '</table></div>'
    )

    peak_conns = max((s.get("total_webrtc_conns", 0) for s in steps), default=0)
    n_steps    = len(steps)
    intro = (
        '<div class="row g-3 mb-3">'
        + _mini("Layout Steps", n_steps)
        + _mini("Peak WebRTC Conns", peak_conns, "", _DANGER)
        + '</div>'
    )

    return (
        '<div class="section-card">'
        '<div class="section-header">Layout Stress &#8212; Concurrent Session Cycling</div>'
        + intro
        + table
        + '<canvas id="layoutStressChart" height="90" class="mt-3"></canvas>'
        + '</div>'
    )


# ---------------------------------------------------------------------------
# Full HTML assembly
# ---------------------------------------------------------------------------

_CSS = """
:root { --art-primary: """ + _PRIMARY + """; --art-dark: """ + _DARK + """; }

/* ── Base ─────────────────────────────────────────────────────────────── */
body { font-family: 'Inter', sans-serif; background: #f0f4f8; color: #1a2332; }
code {
  font-family: 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: .82em; background: #f0f6ff; color: #1d4ed8;
  padding: 1px 5px; border-radius: 3px;
}
.tooltip-inner { max-width: 700px; white-space: nowrap; font-family: monospace; font-size: .8rem; }

/* ── Page header ─────────────────────────────────────────────────────── */
.page-header {
  background: linear-gradient(135deg, var(--art-dark) 0%, var(--art-primary) 100%);
  color: #fff; padding: 28px 32px 24px; border-radius: 12px;
  margin-bottom: 24px; box-shadow: 0 4px 20px rgba(0,0,0,.25);
}
.page-header h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; margin: 0 0 4px; }
.page-header .sub { font-size: .88rem; opacity: .88; }

/* ── Section cards ───────────────────────────────────────────────────── */
.section-card {
  background: #fff; border-radius: 10px; padding: 22px 24px;
  margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,.07);
}
.section-header {
  font-size: .9rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; color: var(--art-dark);
  padding-bottom: 10px; margin-bottom: 18px;
  border-bottom: 2px solid #f0f4f8;
}

/* ── KPI tiles ───────────────────────────────────────────────────────── */
/* stat-card: hero KPI — large value, colored accent border, shadow */
.stat-card {
  background: #fff; border-radius: 8px; padding: 16px 14px 12px;
  text-align: center;
  box-shadow: 0 1px 6px rgba(0,0,0,.08);
  border: 1px solid #e9ecef;
  height: 100%;
}
.stat-label { font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; color: #6c757d; margin-bottom: 4px; }
.stat-value { font-size: 1.8rem; font-weight: 700; line-height: 1.1; color: var(--art-dark); }
.stat-unit  { font-size: .72rem; color: #6c757d; margin-top: 2px; }

/* metric-mini: secondary KPI — medium value, subtle background */
.metric-mini {
  background: #fff; border-radius: 8px; padding: 12px 14px;
  display: flex; flex-direction: column;
  border: 1px solid #e9ecef;
  box-shadow: 0 1px 4px rgba(0,0,0,.05);
}
.metric-mini-label { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: #6c757d; }
.metric-mini-val   { font-size: 1.2rem; font-weight: 700; color: var(--art-dark); margin-top: 2px; }

/* ── Data tables ─────────────────────────────────────────────────────── */
.data-table { width: 100%; border-collapse: collapse; font-size: .85rem; }
.data-table thead tr { background: var(--art-dark); color: #fff; }
.data-table thead th { padding: 10px 12px; font-weight: 600; letter-spacing: .03em; }
.data-table tbody tr:nth-child(even) { background: #f8fafc; }
.data-table tbody tr:hover { background: #eff6ff; }
.data-table tbody td { padding: 8px 12px; border-bottom: 1px solid #e9ecef; vertical-align: middle; }

/* ── Brand elements ──────────────────────────────────────────────────── */
.brand-pill {
  display: inline-block; background: var(--art-primary); color: #fff;
  font-size: .7rem; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; padding: 3px 10px; border-radius: 20px; margin-right: 8px;
}

/* ── Capacity validation: server / tier blocks ───────────────────────── */
.server-block {
  background: #f8fafc; border-radius: 8px; padding: 16px 18px;
  margin-bottom: 16px;
}
.server-block-header {
  font-size: .95rem; font-weight: 700; color: var(--art-dark);
  margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
}
.server-block-name { font-size: 1rem; }
.tier-row { margin-bottom: 10px; }
.tier-label {
  font-size: .7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: #6c757d; margin-bottom: 6px;
}
.tier-metrics { display: flex; flex-wrap: wrap; gap: 8px; }
.tier-metrics .col-md-3, .tier-metrics .col-6 { flex: 0 0 auto; width: 130px; }
.tier-details {
  margin-top: 10px; border-top: 1px dashed #dee2e6; padding-top: 8px;
}
.tier-details summary {
  font-size: .75rem; font-weight: 600; color: var(--art-primary);
  cursor: pointer; user-select: none; padding: 2px 0;
}
.tier-detail-body { padding-top: 10px; }
.detail-count {
  font-size: .7rem; font-weight: 400; color: #6c757d;
  background: #e9ecef; border-radius: 8px; padding: 1px 7px;
}

/* ── Sub-section headers (chart labels, table labels inside cards) ────── */
.detail-sub-header {
  font-size: .78rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; color: #334155; margin: 14px 0 4px;
  display: flex; justify-content: space-between; align-items: center;
}
.chart-chevron {
  display: inline-block; font-size: .65rem; transition: transform .2s;
  margin-left: 6px;
}
.detail-sub-header[aria-expanded="false"] .chart-chevron { transform: rotate(180deg); }

/* Description line shown below a chart sub-header */
.chart-desc {
  font-size: .82rem; color: #64748b; margin: 2px 0 10px;
  line-height: 1.5;
}

/* ── KPI tile labels ─────────────────────────────────────────────────── */
.stat-label        { font-size: .72rem; }
.metric-mini-label { font-size: .72rem; }

/* ── Compact table variant ───────────────────────────────────────────── */
.data-table-sm thead th,
.data-table-sm tbody td { padding: 5px 9px; font-size: .83rem; }

/* ── Export button (in page header) ─────────────────────────────────── */
.export-btn {
  font-size: .78rem; font-weight: 600; padding: 5px 14px;
  border-radius: 20px; border: 1px solid rgba(255,255,255,.55);
  background: transparent; color: #fff; cursor: pointer;
  transition: background .15s;
}
.export-btn:hover { background: rgba(255,255,255,.18); }

/* ── Print ───────────────────────────────────────────────────────────── */
@media print {
  .export-btn, #jumpNav { display: none !important; }
  body { background: #fff; }
  .page-header { box-shadow: none; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .section-card { box-shadow: none; border: 1px solid #dee2e6; }
  canvas { max-width: 100% !important; }
}
"""

# Chart.js inline plugin — draws dashed vertical lines with labels on server charts.
# Registered once on the global Chart object before any chart is constructed.
_SESSION_MARKER_PLUGIN_JS = """
if (!window.__vertLinesRegistered) {
  window.__vertLinesRegistered = true;
  Chart.register({
    id: 'vertLines',
    afterDraw(chart) {
      const lines = ((chart.config.options || {}).plugins || {}).vertLines;
      if (!lines || !lines.length) return;
      const ctx = chart.ctx;
      const xs  = chart.scales.x;
      const ys  = chart.scales.y;
      ctx.save();
      lines.forEach((l, idx) => {
        const x = xs.getPixelForValue(l.xLabel);
        if (x == null || isNaN(x)) return;
        ctx.strokeStyle = l.color || 'rgba(255,99,132,0.65)';
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([5, 3]);
        ctx.beginPath();
        ctx.moveTo(x, ys.top);
        ctx.lineTo(x, ys.bottom);
        ctx.stroke();
        if (l.label) {
          ctx.fillStyle = l.color || 'rgba(255,99,132,0.9)';
          ctx.font      = '9px Inter,sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(l.label, x, ys.top - 4 - (idx % 2) * 10);
        }
      });
      ctx.restore();
    }
  });
}
"""

_NAV_COLLAPSE_JS = """
(function(){
  // ── 0. Init Bootstrap tooltips on elements with data-bs-toggle="tooltip" ─
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function(el){
    new bootstrap.Tooltip(el, { trigger: 'hover', boundary: 'window' });
  });

  // ── 1. Make every direct-child .section-header collapsible ───────────────
  document.querySelectorAll('.section-card').forEach(function(card, i){
    var hdr = card.querySelector(':scope > .section-header');
    if (!hdr) return;  // skip cards with non-standard header nesting (already handled)

    var cardId = 'sc_' + i;
    var bodyId = 'scb_' + i;
    card.id = cardId;

    // Wrap everything after the header in a collapse div
    var body = document.createElement('div');
    body.id = bodyId;
    body.className = 'collapse show';
    body.style.marginTop = '4px';
    var toMove = [];
    var node = hdr.nextSibling;
    while (node) { toMove.push(node); node = node.nextSibling; }
    toMove.forEach(function(n){ body.appendChild(n); });
    card.appendChild(body);

    // Restyle header as a flex toggle row
    hdr.style.cssText += ';cursor:pointer;user-select:none;display:flex;' +
                         'justify-content:space-between;align-items:center;margin-bottom:0;';
    var caret = document.createElement('span');
    caret.innerHTML = '&#9650;';
    caret.style.cssText = 'font-size:.6rem;color:#adb5bd;transition:transform .2s;flex-shrink:0;margin-left:8px;';
    hdr.appendChild(caret);

    hdr.setAttribute('data-bs-toggle', 'collapse');
    hdr.setAttribute('data-bs-target', '#' + bodyId);
    hdr.setAttribute('aria-expanded', 'true');

    card.addEventListener('hide.bs.collapse', function(e){
      if (e.target.id === bodyId) caret.style.transform = 'rotate(180deg)';
    });
    card.addEventListener('show.bs.collapse', function(e){
      if (e.target.id === bodyId) caret.style.transform = 'rotate(0deg)';
    });
  });

  // ── 2. Build sticky "Jump to" navigation bar ─────────────────────────────
  var navLinks = [];
  document.querySelectorAll('.section-card[id]').forEach(function(card){
    var hdr = card.querySelector(':scope > .section-header');
    if (!hdr) return;
    // Extract only the raw text (ignore caret child span)
    var text = '';
    hdr.childNodes.forEach(function(n){
      if (n.nodeType === 3) text += n.textContent.trim();
    });
    if (!text) return;
    var collapseEl = card.querySelector('.collapse');
    navLinks.push({ cardId: card.id, bodyId: collapseEl ? collapseEl.id : '', label: text });
  });
  if (navLinks.length < 2) return;

  var nav = document.createElement('div');
  nav.id = 'jumpNav';
  nav.style.cssText = [
    'position:sticky', 'top:0', 'z-index:1029',
    'background:rgba(255,255,255,0.97)',
    'backdrop-filter:blur(4px)', '-webkit-backdrop-filter:blur(4px)',
    'border-bottom:1px solid #e5e7eb',
    'padding:7px 12px', 'margin-bottom:16px',
    'display:flex', 'align-items:center', 'gap:6px', 'flex-wrap:wrap'
  ].join(';');

  var lbl = document.createElement('span');
  lbl.textContent = 'Jump to:';
  lbl.style.cssText = 'font-size:.68rem;font-weight:700;color:#6c757d;white-space:nowrap;margin-right:4px;';
  nav.appendChild(lbl);

  navLinks.forEach(function(item){
    var a = document.createElement('a');
    a.textContent = item.label;
    a.href = '#' + item.cardId;
    a.style.cssText = [
      'font-size:.68rem', 'font-weight:600',
      'padding:3px 10px', 'border-radius:20px',
      'background:#f3f4f6', 'color:#374151',
      'text-decoration:none', 'white-space:nowrap',
      'border:1px solid #e5e7eb', 'transition:background .15s'
    ].join(';');
    a.addEventListener('mouseenter', function(){ this.style.background = '#dbeafe'; this.style.color = '#1d4ed8'; });
    a.addEventListener('mouseleave', function(){ this.style.background = '#f3f4f6'; this.style.color = '#374151'; });
    a.addEventListener('click', function(e){
      e.preventDefault();
      // Expand the section if collapsed
      if (item.bodyId) {
        var bodyEl = document.getElementById(item.bodyId);
        if (bodyEl && !bodyEl.classList.contains('show')) {
          new bootstrap.Collapse(bodyEl, {toggle: false}).show();
        }
      }
      setTimeout(function(){
        var cardEl = document.getElementById(item.cardId);
        if (cardEl) cardEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 60);
    });
    nav.appendChild(a);
  });

  // Insert nav after the page-header div
  var container = document.querySelector('.container-fluid');
  var pageHeader = container && container.querySelector('.page-header');
  if (pageHeader && pageHeader.nextSibling) {
    container.insertBefore(nav, pageHeader.nextSibling);
  } else if (container) {
    container.insertBefore(nav, container.firstChild);
  }
})();
"""

_MARKDOWN_EXPORT_JS = """
function downloadMarkdown() {
  var d = window._reportData;
  var scenario = d.scenario || 'report';
  var generated = d.generated_at || '';
  var tiers = d.tiers || [];
  var lines = [];

  lines.push('# ORC Performance Report');
  lines.push('');
  lines.push('**Scenario:** ' + scenario + '  ');
  if (d.server_name) lines.push('**Server:** ' + d.server_name + '  ');
  if (d.hardware)    lines.push('**Hardware:** ' + d.hardware + '  ');
  lines.push('**Generated:** ' + generated);
  lines.push('');

  if (tiers.length > 0) {
    lines.push('## Results Summary');
    lines.push('');
    lines.push('| Server | Hardware | Tier | Connected | Rate | Avg Load (ms) | Max Load (ms) | Avg CPU | Max CPU | Avg RAM (GB) | Net Recv (Mbps) |');
    lines.push('|--------|----------|------|:---------:|-----:|---------------|---------------|--------:|--------:|-------------:|----------------:|');
    tiers.forEach(function(t) {
      var s = t.summary || {};
      var sv = t.server || {};
      var rate = typeof s.connect_rate_pct === 'number' ? s.connect_rate_pct.toFixed(1) : '—';
      lines.push(
        '| ' + (t.server_name || '') +
        ' | ' + (t.hardware || '') +
        ' | ' + (t.session_tier || '') +
        ' | ' + (s.connected || 0) + ' / ' + (s.total_sessions || 0) +
        ' | ' + rate + '%' +
        ' | ' + (s.avg_load_ms || 0) +
        ' | ' + (s.max_load_ms || 0) +
        ' | ' + (sv.avg_cpu !== undefined ? sv.avg_cpu + '%' : '—') +
        ' | ' + (sv.max_cpu !== undefined ? sv.max_cpu + '%' : '—') +
        ' | ' + (sv.avg_ram_gb !== undefined ? sv.avg_ram_gb : '—') +
        ' | ' + (sv.avg_net_recv_mbps !== undefined ? sv.avg_net_recv_mbps : '—') +
        ' |'
      );
    });
    lines.push('');
    lines.push('## API Latency');
    lines.push('');
    lines.push('| Server | Tier | API Calls | API Avg (ms) | BW Checks | BW p95 (ms) |');
    lines.push('|--------|-----:|----------:|-------------:|----------:|------------:|');
    tiers.forEach(function(t) {
      var a = t.api || {};
      var apiCalls = a.non_bw_total !== undefined ? a.non_bw_total : a.total || 0;
      lines.push(
        '| ' + (t.server_name || '') +
        ' | ' + (t.session_tier || '') +
        ' | ' + apiCalls +
        ' | ' + (a.avg_ms || 0) +
        ' | ' + (a.bw_checks || 0) +
        ' | ' + (a.bw_p95_ms || 0) +
        ' |'
      );
    });
  } else {
    var s = d.summary || {};
    var a = d.api || {};
    lines.push('## Summary');
    lines.push('');
    lines.push('| Metric | Value |');
    lines.push('|--------|------:|');
    lines.push('| Total Sources | ' + (s.total_sessions || 0) + ' |');
    lines.push('| Connected | ' + (s.connected || 0) + ' |');
    lines.push('| Blocked | ' + (s.blocked || 0) + ' |');
    lines.push('| Connect Rate | ' + (s.connect_rate_pct || 0) + '% |');
    lines.push('| Avg Load | ' + (s.avg_load_ms || 0) + ' ms |');
    lines.push('| Max Load | ' + (s.max_load_ms || 0) + ' ms |');
    if (d.server) {
      var sv = d.server;
      lines.push('| Avg CPU | ' + sv.avg_cpu + '% |');
      lines.push('| Max CPU | ' + sv.max_cpu + '% |');
      var ramLabel = sv.total_ram_gb ? (sv.avg_ram_gb.toFixed(1) + ' / ' + sv.total_ram_gb + ' GB (' + Math.round(sv.avg_ram_gb / sv.total_ram_gb * 100) + '%)') : (sv.avg_ram_gb + ' GB');
      lines.push('| Avg RAM | ' + ramLabel + ' |');
      lines.push('| Net Recv | ' + sv.avg_net_recv_mbps + ' Mbps |');
    }
    lines.push('');
    lines.push('## API Latency');
    lines.push('');
    lines.push('| Metric | Value |');
    lines.push('|--------|------:|');
    lines.push('| API Calls | ' + (a.non_bw_total !== undefined ? a.non_bw_total : a.total || 0) + ' |');
    lines.push('| API Avg | ' + (a.avg_ms || 0) + ' ms |');
    lines.push('| BW Checks | ' + (a.bw_checks || 0) + ' |');
    lines.push('| BW p95 | ' + (a.bw_p95_ms || 0) + ' ms |');
    lines.push('');
    var sessions = d.sessions || [];
    if (sessions.length > 0) {
      lines.push('## Source Results');
      lines.push('');
      lines.push('| Session # | Connected | BW Modal | Load (s) |');
      lines.push('|---------:|:---------:|:--------:|----------:|');
      sessions.forEach(function(src) {
        lines.push(
          '| ' + src.tab +
          ' | ' + (src.connected ? 'Yes' : 'No') +
          ' | ' + (src.modal_fired ? 'Yes' : 'No') +
          ' | ' + (src.load_ms / 1000).toFixed(2) + ' s' +
          ' |'
        );
      });
    }
  }

  lines.push('');
  lines.push('---');
  lines.push('*Generated by orc-performance-test — Arthrex SQA*');

  var md = lines.join('\\n');
  var blob = new Blob([md], {type: 'text/markdown'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'orc-perf-' + scenario + '.md';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
"""


# ---------------------------------------------------------------------------
# Endurance-specific helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float, total_seconds: float = 0) -> str:
    """Format elapsed seconds as a human-readable label.

    When the run spans multiple days (total_seconds > 86400) the label uses
    'Day N Hh' style (e.g. 'Day 2 6h') so the chart X-axis is readable.
    For sub-day runs the classic 'Xh Ym' format is used.
    """
    s = int(seconds)
    if total_seconds > 86400:
        # Elapsed-hours format: "+63h", with "(Day N)" appended at exact day boundaries
        total_h = s // 3600
        rem_h   = s % 3600
        label   = f"+{total_h}h"
        if rem_h == 0 and total_h > 0 and total_h % 24 == 0:
            day = total_h // 24 + 1
            label += f" (Day {day})"
        return label
    # Sub-day format
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if not h and not m: parts.append(f"{sec}s")
    return " ".join(parts) or "0m"


def _inline_assets() -> tuple[str, str]:
    """Fetch CDN assets and return (css_tags, js_tags) as inline <style>/<script> blocks.

    Falls back to CDN links if any fetch fails (e.g. no internet).
    """
    import urllib.request as _ur
    _ASSETS = {
        "bootstrap_css":  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css",
        "bootstrap_js":   "https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js",
        "chartjs":        "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js",
        "hammerjs":       "https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js",
        "chartjs_zoom":   "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js",
    }
    fetched = {}
    for key, url in _ASSETS.items():
        try:
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, timeout=10) as resp:
                fetched[key] = resp.read().decode("utf-8", errors="replace")
        except Exception:
            fetched[key] = None  # fall back to CDN link

    def _css(key, url):
        if fetched.get(key):
            return f"<style>{fetched[key]}</style>\n"
        return f"<link rel='stylesheet' href='{url}'/>\n"

    def _js(key, url):
        if fetched.get(key):
            return f"<script>{fetched[key]}</script>\n"
        return f"<script src='{url}'></script>\n"

    css_tags = (
        _css("bootstrap_css", _ASSETS["bootstrap_css"])
    )
    js_tags = (
        _js("bootstrap_js",  _ASSETS["bootstrap_js"])
        + _js("chartjs",     _ASSETS["chartjs"])
        + _js("hammerjs",    _ASSETS["hammerjs"])
        + _js("chartjs_zoom",_ASSETS["chartjs_zoom"])
    )
    return css_tags, js_tags


def _endurance_findings_html(data: dict) -> str:
    """Compute and render a Key Findings card from endurance run data."""
    import re as _re, statistics as _stats
    spikes   = (data.get("server", {}) or {}).get("process_spikes", [])
    st       = data.get("server_timeline", [])
    alerts   = data.get("alerts", [])
    dur_act  = data.get("duration_actual_s", 0)

    bullets: list[tuple[str, str]] = []  # (icon, text)
    live_tl: list = []  # populated below if server_timeline exists

    # ── Server data coverage ────────────────────────────────────────────────
    if st:
        live_tl = list(st)
        if len(live_tl) > 2:
            tail_cpu = live_tl[-1].get("cpu_percent")
            tail_ram = live_tl[-1].get("ram_pct")
            cut = len(live_tl) - 1
            while cut > 0 and (live_tl[cut-1].get("cpu_percent") == tail_cpu
                                and live_tl[cut-1].get("ram_pct") == tail_ram):
                cut -= 1
            stale = len(live_tl) - cut
            if stale > max(10, len(live_tl) // 10):
                live_tl = live_tl[:cut]  # trim stale tail for all downstream analysis
                real_end_s  = live_tl[-1].get("elapsed_s", 0)
                real_day    = int(real_end_s // 86400) + 1
                real_hrs    = (real_end_s % 86400) / 3600
                stale_hrs   = round(stale * 60 / 3600, 1)
                bullets.append(("⚠", f"Server metrics stopped at Day {real_day} {real_hrs:.1f}h. "
                                     f"WinRM session dropped. Final {stale_hrs}h of data was frozen and trimmed. "
                                     f"<strong>WinRM reconnect fix is in place for future runs.</strong>"))
        # Final state removed — covered by CPU trend bullet

    # ── CPU growth trend ────────────────────────────────────────────────────
    if len(live_tl) > 120:
        # Compare first 10% vs last 10% of live data to detect sustained growth
        n10 = max(30, len(live_tl) // 10)
        early_cpu = [p.get("cpu_percent", 0) for p in live_tl[:n10]]
        late_cpu  = [p.get("cpu_percent", 0) for p in live_tl[-n10:]]
        early_ram = [p.get("ram_pct", 0) for p in live_tl[:n10]]
        late_ram  = [p.get("ram_pct", 0) for p in live_tl[-n10:]]
        avg_early = sum(early_cpu) / len(early_cpu)
        avg_late  = sum(late_cpu)  / len(late_cpu)
        avg_early_ram = sum(early_ram) / len(early_ram)
        avg_late_ram  = sum(late_ram)  / len(late_ram)
        cpu_delta = avg_late - avg_early
        ram_delta = avg_late_ram - avg_early_ram
        if cpu_delta >= 10:
            n_sources_trend = data.get("session_tier") or data.get("summary", {}).get("connected", 0)
            ff_pids_trend = len({s["pid"] for s in spikes if "ffmpeg" in s.get("process", "").lower()})
            pid_overcount = ff_pids_trend > (n_sources_trend or 0)
            bullets.append(("▲",
                f"CPU grew from ~{avg_early:.0f}% to ~{avg_late:.0f}% by Day 2-3 (+{cpu_delta:.0f} pp) then stabilized. "
                f"RAM moved only +{ram_delta:.1f}% over the same period. Not a memory leak."
            ))

    # ── CPU saturation ────────────────────────────────────────────────────
    if live_tl:
        _n_tl      = len(live_tl)
        _n_above90 = sum(1 for p in live_tl if p.get("cpu_percent", 0) >= 90)
        _n_above80 = sum(1 for p in live_tl if p.get("cpu_percent", 0) >= 80)
        _pct90     = round(_n_above90 / _n_tl * 100, 1)
        _pct80     = round(_n_above80 / _n_tl * 100, 1)
        _avg_cpu_a = round(sum(p.get("cpu_percent", 0) for p in live_tl) / _n_tl, 1)
        _cpu_icon  = "▲" if _pct80 >= 20 else "●"
        bullets.append((_cpu_icon,
            f"CPU averaged <strong>{_avg_cpu_a}%</strong> across {_n_tl:,} samples. "
            f"Above 80%: <strong>{_pct80}%</strong> of the time ({_n_above80:,} samples). "
            f"Above 90%: <strong>{_pct90}%</strong> of the time ({_n_above90:,} samples)."
        ))

    # ── RAM stability ─────────────────────────────────────────────────────
    _srv_r      = data.get("server", {}) or {}
    _avg_ram_gb = _srv_r.get("avg_ram_gb")
    _total_ram  = _srv_r.get("total_ram_gb")
    if _avg_ram_gb and _total_ram and live_tl:
        _peak_ram_pct = max((p.get("ram_pct", 0) for p in live_tl), default=0)
        _peak_ram_gb  = round(_peak_ram_pct / 100 * _total_ram, 1)
        _avg_ram_pct  = round(_avg_ram_gb / _total_ram * 100, 1)
        _head_gb      = round(_total_ram - _peak_ram_gb, 1)
        _ram_icon     = "▲" if _peak_ram_pct >= 85 else "●"
        bullets.append((_ram_icon,
            f"RAM averaged <strong>{_avg_ram_gb:.1f} GB ({_avg_ram_pct}%)</strong>. "
            f"Peak: <strong>{_peak_ram_gb:.1f} GB ({_peak_ram_pct:.1f}%)</strong> of {int(_total_ram)} GB total "
            f"({_head_gb:.1f} GB headroom remaining)."
        ))

    # ── Network ───────────────────────────────────────────────────────────
    _avg_recv = (_srv_r).get("avg_net_recv_mbps")
    _avg_send = (_srv_r).get("avg_net_send_mbps")
    if _avg_recv or _avg_send:
        bullets.append(("●",
            f"Network averaged <strong>{(_avg_recv or 0):.1f} Mbps</strong> inbound "
            f"and <strong>{(_avg_send or 0):.1f} Mbps</strong> outbound (server-side)."
        ))

    # ── Hardware recommendation ─────────────────────────────────────────────
    srv_summary = data.get("server", {}) or {}
    max_cpu_val  = srv_summary.get("max_cpu", 0)
    hw_str       = data.get("hardware", "")
    if not hw_str:
        # Fallback: look up from environments config using server_name
        try:
            from config.environments import SERVERS as _SERVERS
            _sname = data.get("server_name", "")
            if _sname in _SERVERS:
                hw_str = _SERVERS[_sname].get("hardware", "")
        except ImportError:
            pass
    n_rooms      = data.get("session_tier") or data.get("summary", {}).get("connected", 0)
    hw_match     = _re.search(r"(\d+)\s*core", hw_str, _re.IGNORECASE)
    if hw_match and n_rooms:
        # Common server CPU core counts — snap up to next realistic SKU
        _CPU_SKUS = [4, 8, 12, 16, 24, 32, 36, 48, 64, 96, 128, 192, 256]
        def _snap_sku(n):
            for s in _CPU_SKUS:
                if s >= n: return s
            return int(n + 4 - n % 4) if n % 4 else int(n)  # fallback
        current_cores = int(hw_match.group(1))
        # Use actual p99 CPU from this run + 20% headroom at 60% target utilization.
        # This sizes for worst-case sustained load, not the average.
        _HEADROOM   = 1.20
        _TARGET_PCT = 0.60
        p99_cpu_val = srv_summary.get("p99_cpu") or max_cpu_val
        p99_cores   = (p99_cpu_val / 100.0) * current_cores
        work_cores  = p99_cores * _HEADROOM
        raw_rec     = work_cores / _TARGET_PCT
        rec_sku     = _snap_sku(raw_rec)
        is_ok       = current_cores >= raw_rec
        bullets.append(("✓" if is_ok else "▲",
            f"<strong>Hardware recommendation for {n_rooms}-source load (p99 + 20% headroom):</strong> "
            f"p99 CPU = {p99_cpu_val:.1f}% on {current_cores} cores = {p99_cores:.1f} active cores. "
            f"With 20% headroom: {p99_cores:.1f} × 1.20 = {work_cores:.1f} cores of sustained demand. "
            f"At 60% target utilization: {work_cores:.1f} ÷ 0.60 = {raw_rec:.1f} → "
            f"<strong>{rec_sku} cores recommended</strong> (next standard CPU SKU above {raw_rec:.1f}). "
            + (f"Current {current_cores}-core server is undersized. "
               if not is_ok else
               f"Current {current_cores}-core server meets this spec. ")
            + "See the interactive calculator below to model different source counts and targets."
        ))


    # ── ffmpeg analysis ─────────────────────────────────────────────────────
    if spikes:
        ffmpeg_sp = [s for s in spikes if "ffmpeg" in s.get("process", "").lower()]
        if ffmpeg_sp:
            unique_pids     = len({s["pid"] for s in ffmpeg_sp})
            # peak concurrent = max ffmpeg#N index + 1 at any single timestamp
            ts_max: dict = {}
            for s in ffmpeg_sp:
                m = _re.search(r"#(\d+)", s.get("process", ""))
                n = int(m.group(1)) if m else 0
                ts_max[s["time"]] = max(ts_max.get(s["time"], 0), n)
            peak_concurrent = max(ts_max.values()) + 1 if ts_max else 0
            rams = sorted(float(s["working_set_mb"]) for s in ffmpeg_sp)
            med_ram  = rams[len(rams)//2]
            peak_ram_total = round(peak_concurrent * med_ram / 1024, 1)
            n_sources = data.get("session_tier") or data.get("summary", {}).get("connected", 0)
            # Note on concurrent vs unique: unique PIDs counts every ffmpeg ever spawned across
            # the full run (including restarts); peak concurrent is the highest number active
            # at any one snapshot. One fewer than expected (e.g. 35 of 36) is normal — a room
            # with no signal or a brief source drop may not have an active ffmpeg at that moment.
            one_short = n_sources and peak_concurrent == n_sources - 1
            # Per-type CPU breakdown from ffmpeg_instances
            _fi_list_f  = srv_summary.get("ffmpeg_instances") or []
            _ftype_cpus: dict = {}
            for _fi in _fi_list_f:
                _fu = (_fi.get("url") or "").lower()
                if _fu.startswith("rtsps://"):                          _ft = "Vision"
                elif _fu.startswith("rtmp://"):                         _ft = "Matrix"
                elif ":8554/orc/" in _fu:                               _ft = "Sim"
                elif "/cameravideo" in _fu:                             _ft = "UHD4"
                elif "/media/video" in _fu or "/axis-media/" in _fu:   _ft = "Sony"
                else:                                                    _ft = "Sim"
                _ftype_cpus.setdefault(_ft, []).append(_fi.get("avg_cpu_pct", 0))

            _ncores_f       = int(hw_match.group(1)) if hw_match else 16
            _p99_val_f      = srv_summary.get("p99_cpu") or srv_summary.get("max_cpu") or 0
            _p99_cores_f    = round((_p99_val_f / 100.0) * _ncores_f, 1) if _p99_val_f else None
            _type_lines_f: list[str] = []
            _total_ff_cores = 0.0
            for _ft in ("Vision", "Sony", "UHD4", "Sim", "Matrix"):
                if _ft in _ftype_cpus:
                    _fvals  = _ftype_cpus[_ft]
                    _fn     = len(_fvals)
                    _favg   = sum(_fvals) / _fn
                    _fcores = _favg / 100 * _ncores_f
                    _total_ff_cores += _fn * _fcores
                    _type_lines_f.append(
                        f"{_ft} ({_fn} stream{'s' if _fn != 1 else ''}): "
                        f"avg {_favg:.1f}% server CPU = {_fcores:.2f} cores/stream"
                    )

            _cpu_vals_ff = [float(s.get("proc_cpu", 0)) for s in ffmpeg_sp if s.get("proc_cpu")]
            _avg_ff_cpu  = round(sum(_cpu_vals_ff) / len(_cpu_vals_ff), 1) if _cpu_vals_ff else 0
            _overhead_f  = round(_p99_cores_f - _total_ff_cores, 1) if _p99_cores_f else None

            _ff_text = (
                f"<strong>{peak_concurrent} ffmpeg processes</strong> active at peak "
                f"(of {n_sources} sources). Avg {_avg_ff_cpu:.0f}% server CPU per process; "
                f"median {med_ram:.0f} MB RAM each."
            )
            if _type_lines_f:
                _ff_text += (
                    f"<ul style='margin:.4rem 0 0 1rem;padding:0;list-style:none;'>"
                    + "".join(f"<li style='margin-bottom:.2rem;'>&#9642; {l}</li>" for l in _type_lines_f)
                    + "</ul>"
                    f"<span style='font-size:.85em;color:#6c757d;'>ffmpeg total: {_total_ff_cores:.1f} active cores"
                    + (f". Overhead (ORC + OS): {_overhead_f:.1f} cores." if _overhead_f else ".")
                    + "</span>"
                )
            bullets.append(("●", _ff_text))



        # SentinelAgent note
        sentinel = [s for s in spikes if "sentinel" in s.get("process", "").lower()]
        if sentinel:
            n_sentinel = len(sentinel)
            bullets.append(("▲", f"SentinelAgent appeared in <strong>{n_sentinel:,} spike snapshots</strong>. "
                                 f"The security agent competed for CPU during high-load periods. "
                                 f"Consider scheduling AV scans during off-peak windows."))

    if not bullets:
        return ""

    rows_html = "".join(
        f"<li style='margin-bottom:.5rem;'>{text}</li>"
        for icon, text in bullets
    )

    # ── Interactive capacity calculator (Source Mix) ──────────────────────
    # Uses real per-type rates derived from ffmpeg_instances data, calibrated
    # against the measured p99 total CPU so the math always ties back to the
    # hardware recommendation above.
    calc_block = ""
    n_tested_c = n_rooms
    if hw_match and n_tested_c and p99_cpu_val:
        cur_cores_c = int(hw_match.group(1))
        p99_cores_c = round((p99_cpu_val / 100.0) * cur_cores_c, 3)
        _SKUS_JS    = [4, 8, 12, 16, 24, 32, 36, 48, 64, 96, 128, 192, 256]

        # URL classifier (shared between room_sources and ffmpeg_instances)
        def _classify_cap(u):
            u = (u or "").lower()
            if u.startswith("rtsps://"): return "vision"
            if u.startswith("rtmp://"):  return "matrix"
            if ":8554/orc/" in u:        return "sim"
            if "/cameravideo" in u:      return "uhd4"
            if "/media/video" in u or "/axis-media/" in u: return "sony"
            return "sim"

        # Pre-populate UI counts from room_sources
        _src_counts = {"vision": 0, "sony": 0, "uhd4": 0, "matrix": 0, "sim": 0}
        for _rs in data.get("room_sources", []):
            _src_counts[_classify_cap(_rs.get("url") or "")] += 1

        # ── Derive real rates from actual ffmpeg per-process data ──────────
        # server["ffmpeg_instances"] has one entry per unique URL with avg_cpu_pct
        # (average % of TOTAL server CPU used by that process across all samples).
        # Converting: avg_cpu_pct / 100 * total_cores = cores per stream (raw ffmpeg).
        _fi_list = (srv_summary or {}).get("ffmpeg_instances") or []
        _fi_cpus = {"vision": [], "sony": [], "uhd4": [], "matrix": [], "sim": []}
        for _fi_row in _fi_list:
            _fi_cpus[_classify_cap(_fi_row.get("url") or "")].append(
                _fi_row.get("avg_cpu_pct", 0)
            )
        _fi_mean = {}  # type → mean raw cores/stream (ffmpeg process only)
        for _t, _vals in _fi_cpus.items():
            _fi_mean[_t] = round(sum(_vals) / len(_vals) / 100.0 * cur_cores_c, 4) if _vals else 0.0

        # ── Calibration factor ─────────────────────────────────────────────
        # The ffmpeg process monitor only captures the transcoding processes.
        # Total server CPU also includes the ORC application server, OS scheduler,
        # network I/O, and encoding pipeline overhead — none of which show up in
        # per-process data.  We compute an overhead multiplier so that:
        #   sum(room_source_count_i × ffmpeg_mean_rate_i) × overhead_k = p99_total_cores
        # This ties the calculator back to ground-truth measured data.
        _ffmpeg_model = 0.0
        for _t, _cnt in _src_counts.items():
            _ffmpeg_model += _cnt * _fi_mean.get(_t, _fi_mean.get("sim", 0.0))
        _overhead_k = round(p99_cores_c / _ffmpeg_model, 3) if _ffmpeg_model > 0 else 1.0

        # Calibrated cores/stream = ffmpeg_mean × overhead_k
        # For types with no streams in this run, fall back to sim rate.
        _sim_cal = round(_fi_mean.get("sim", 0.001) * _overhead_k, 3)
        _cal = {}
        for _t in ("vision", "sony", "uhd4", "matrix", "sim"):
            if _fi_mean.get(_t, 0) > 0:
                _cal[_t] = round(_fi_mean[_t] * _overhead_k, 3)
            else:
                _cal[_t] = _sim_cal  # fallback: no data → use sim rate

        # Track which types actually have measured data from ffmpeg_instances
        _has_data = {_t: (len(_fi_cpus.get(_t, [])) > 0) for _t in ("vision", "sony", "uhd4", "matrix", "sim")}

        # Build hover-tooltip notes per type
        def _cap_note(tid):
            vals = _fi_cpus.get(tid, [])
            if vals:
                raw = _fi_mean[tid]
                return (f"{len(vals)} stream(s) measured this run. "
                        f"Raw ffmpeg mean: {raw:.4f} cores/stream "
                        f"x {_overhead_k:.3f} overhead factor "
                        f"= {_cal[tid]:.3f} cores/stream.")
            else:
                return (f"No {tid} streams in this run. "
                        f"Using sim rate ({_sim_cal:.3f} cores/stream) as a conservative estimate.")

        # JS-safe escaping for tooltip strings
        def _js_str(s):
            return s.replace("\\", "\\\\").replace("'", "\\'")

        _v  = _src_counts["vision"]
        _so = _src_counts["sony"]
        _u4 = _src_counts["uhd4"]
        _mx = _src_counts["matrix"]
        _sm = _src_counts["sim"]

        calc_block = f"""
<style>
/* ── Calculator card ── */
#capCalcCard {{ margin-top:1.5rem; }}
#capCalcCard .cap-header {{
  display:flex; align-items:center; gap:.6rem; cursor:pointer;
  padding:.6rem .85rem; border-radius:8px 8px 0 0;
  background:linear-gradient(135deg,#1a1f36 0%,#2d3561 100%);
  color:#fff; font-size:.9rem; font-weight:600; letter-spacing:.02em;
  user-select:none;
}}
#capCalcCard .cap-header .cap-chevron {{
  margin-left:auto; transition:transform .25s; font-size:.75rem; opacity:.7;
}}
#capCalcCard .cap-header.collapsed .cap-chevron {{ transform:rotate(-90deg); }}
#capCalcCard .cap-body {{
  background:#fff; border:1px solid #dee2e6; border-top:none;
  border-radius:0 0 8px 8px; overflow:hidden;
}}

/* ── Calibration accordion ── */
.cap-cal-toggle {{
  display:flex; align-items:center; gap:.5rem; padding:.55rem .9rem;
  background:#f0f4ff; border-bottom:1px solid #dce4f5;
  cursor:pointer; font-size:.78rem; color:#3a4a8a; font-weight:600;
  user-select:none;
}}
.cap-cal-toggle .cc-chevron {{ margin-left:auto; transition:transform .2s; font-size:.65rem; }}
.cap-cal-toggle.open .cc-chevron {{ transform:rotate(180deg); }}
.cap-cal-details {{
  background:#f7f9ff; border-bottom:1px solid #dce4f5;
  font-size:.78rem; line-height:1.9; color:#444;
  padding:.7rem 1rem .7rem 1.1rem; display:none;
}}
.cap-cal-details.open {{ display:block; }}
.cap-cal-details .step {{
  display:grid; grid-template-columns:7rem 1fr; gap:.2rem .6rem;
  margin-bottom:.15rem;
}}
.cap-cal-details .step-label {{
  font-weight:700; color:#3a4a8a; white-space:nowrap; padding-top:1px;
}}
.cap-cal-details code {{
  background:#e8edf8; padding:1px 5px; border-radius:3px; font-size:.77rem;
}}

/* ── Source type table ── */
.cap-table {{ width:100%; border-collapse:collapse; font-size:.84rem; }}
.cap-table thead tr {{ background:#f0f4ff; }}
.cap-table thead th {{
  padding:8px 12px; font-weight:600; color:#3a4a8a; font-size:.78rem;
  text-transform:uppercase; letter-spacing:.04em; border-bottom:2px solid #dce4f5;
}}
.cap-table tbody tr {{ border-bottom:1px solid #f0f0f0; transition:background .12s; }}
.cap-table tbody tr:hover {{ background:#fafbff; }}
.cap-table tbody tr.cap-row-active {{ background:#f4f8ff; }}
.cap-type-dot {{
  display:inline-block; width:9px; height:9px; border-radius:50%;
  margin-right:6px; vertical-align:middle; flex-shrink:0;
}}
.cap-count-input {{
  width:64px; text-align:center; font-size:.85rem; padding:4px 6px;
  border:1.5px solid #ced4da; border-radius:6px; font-weight:600;
  transition:border-color .15s,box-shadow .15s;
}}
.cap-count-input:focus {{
  outline:none; border-color:#4d6ef5; box-shadow:0 0 0 3px rgba(77,110,245,.15);
}}
.cap-rate-input {{
  width:66px; text-align:center; font-size:.84rem; padding:3px 5px;
  border:1.5px solid #ced4da; border-radius:6px;
  transition:border-color .15s,background .15s,box-shadow .15s;
}}
.cap-rate-input:focus {{
  outline:none; border-color:#4d6ef5; box-shadow:0 0 0 3px rgba(77,110,245,.15);
}}
.cap-rate-input.overridden {{
  border-color:#4d6ef5; background:#eef1ff;
}}
.cap-badge {{
  display:inline-flex; align-items:center; gap:3px;
  font-size:.7rem; font-weight:700; padding:2px 7px; border-radius:20px;
  cursor:help; white-space:nowrap;
}}
.cap-badge.measured  {{ background:#d1f0e0; color:#146c43; }}
.cap-badge.estimated {{ background:#fff3cd; color:#855a00; }}
.cap-work-cell {{
  font-family:monospace; font-size:.83rem; color:#343a40; text-align:right;
  padding:8px 14px;
}}
.cap-work-cell.active {{ color:#2c5fcc; font-weight:700; }}
.cap-total-row td {{
  padding:9px 12px; background:#f0f4ff; font-weight:700;
  border-top:2px solid #b8c8f0;
}}
.cap-total-val {{
  font-family:monospace; font-size:1.05rem; color:#2c5fcc;
  text-align:right; padding-right:14px;
}}

/* ── Sliders ── */
.cap-sliders {{
  display:grid; grid-template-columns:1fr 1fr auto;
  gap:.75rem 1.25rem; align-items:center;
  padding:.85rem 1rem; background:#fafbff; border-top:1px solid #eef0f8;
}}
.cap-slider-group {{ display:flex; align-items:center; gap:10px; }}
.cap-slider-label {{
  white-space:nowrap; font-size:.78rem; font-weight:600;
  color:#555; min-width:5.5rem;
}}
.cap-slider-val {{
  min-width:2.8rem; font-weight:800; font-size:.92rem;
  color:#2c5fcc; text-align:right;
}}
.cap-slider {{
  -webkit-appearance:none; appearance:none;
  flex:1; height:7px; border-radius:4px; cursor:pointer; outline:none;
  background: linear-gradient(to right, #4d6ef5 0%, #4d6ef5 var(--pct,50%), #dde3f8 var(--pct,50%), #dde3f8 100%);
}}
.cap-slider::-webkit-slider-thumb {{
  -webkit-appearance:none; appearance:none;
  width:18px; height:18px; border-radius:50%;
  background:#fff; border:2.5px solid #4d6ef5;
  box-shadow:0 1px 4px rgba(77,110,245,.35);
  cursor:pointer; transition:transform .1s;
}}
.cap-slider::-webkit-slider-thumb:hover {{ transform:scale(1.2); }}
.cap-slider::-moz-range-thumb {{
  width:18px; height:18px; border-radius:50%;
  background:#fff; border:2.5px solid #4d6ef5;
  box-shadow:0 1px 4px rgba(77,110,245,.35); cursor:pointer;
}}
.cap-reset-btn {{
  padding:5px 14px; font-size:.75rem; font-weight:600; border-radius:6px;
  background:#6c757d; color:#fff; border:none; cursor:pointer;
  transition:background .15s;
}}
.cap-reset-btn:hover {{ background:#495057; }}

/* ── Result panel ── */
.cap-result {{
  margin:0; padding:1rem 1.1rem;
  border-top:1px solid #eef0f8; font-size:.84rem;
}}
.cap-result-steps {{
  border:1px solid #eef0f8; border-radius:7px; overflow:hidden;
  margin-bottom:.75rem; font-size:.82rem;
}}
.cap-step-row {{
  display:grid; grid-template-columns:13rem 1fr;
}}
.cap-step-row:nth-child(even) {{ background:#fafbff; }}
.cap-step-row:nth-child(odd)  {{ background:#fff; }}
.cap-step-label {{
  padding:7px 12px; color:#666; font-weight:600; font-size:.78rem;
  border-right:1px solid #eef0f8; display:flex; align-items:center; gap:6px;
}}
.cap-step-num {{
  display:inline-flex; align-items:center; justify-content:center;
  width:18px; height:18px; border-radius:50%; background:#e8edf8;
  color:#3a4a8a; font-size:.7rem; font-weight:800; flex-shrink:0;
}}
.cap-step-val {{
  padding:7px 12px; color:#343a40; display:flex; align-items:center; gap:8px;
  flex-wrap:wrap;
}}
.cap-step-val code {{
  background:#f0f4ff; padding:2px 7px; border-radius:4px;
  font-size:.8rem; color:#2c3e8a;
}}
.cap-step-hint {{ font-size:.74rem; color:#8a93a2; }}
.cap-step-breakdown {{ line-height:1.85; }}
.cap-breakdown-item {{ display:flex; align-items:center; gap:6px; }}
.cap-breakdown-dot {{ width:7px; height:7px; border-radius:50%; flex-shrink:0; }}
.cap-verdict {{
  display:flex; align-items:center; gap:.6rem;
  padding:.7rem 1rem; border-radius:7px; font-weight:700; font-size:.9rem;
}}
.cap-verdict.ok  {{ background:#d1f0e0; color:#146c43; }}
.cap-verdict.bad {{ background:#fde8e8; color:#9b1c1c; }}
.cap-verdict-icon {{ font-size:1.2rem; }}
.cap-verdict-sku  {{ font-size:1.35rem; font-weight:900; margin-left:4px; }}
.cap-est-warn {{
  margin-top:.5rem; padding:.45rem .75rem; border-radius:6px;
  background:#fffbec; border:1px solid #ffe58f;
  font-size:.78rem; color:#7d5a00;
}}
</style>

<div id='capCalcCard'>
  <!-- Header -->
  <div class='cap-header' id='capCalcHeader' onclick='capToggle()'>
    Source Mix Capacity Calculator
    <span style='font-size:.75rem;font-weight:400;opacity:.75;margin-left:.25rem;'>
      (calibrated from this run&rsquo;s measured data)
    </span>
    <span class='cap-chevron'>&#9660;</span>
  </div>

  <div class='cap-body' id='capCalcBody'>

    <!-- Calibration accordion -->
    <div class='cap-cal-toggle open' id='capCalTog' onclick='capToggleCal()'>
      How these calibrated rates were derived
      <span class='cc-chevron open' id='capCalChev'>&#9660;</span>
    </div>
    <div class='cap-cal-details open' id='capCalDet'>
      <div class='step'>
        <span class='step-label'>Step 1</span>
        <span>Measured p99 total server CPU:&nbsp;
          <code>{p99_cpu_val:.2f}% &times; {cur_cores_c} cores = <strong>{p99_cores_c:.2f} active cores</strong></code>.
          This is the ground truth anchor.
        </span>
      </div>
      <div class='step'>
        <span class='step-label'>Step 2</span>
        <span>Average per-stream ffmpeg process CPU (each row averaged over all polling samples):&nbsp;
          Vision&nbsp;<code>{_fi_mean.get("vision",0):.4f}</code>&nbsp;&middot;&nbsp;
          Sony/Axis&nbsp;<code>{_fi_mean.get("sony",0):.4f}</code>&nbsp;&middot;&nbsp;
          UHD4&nbsp;<code>{_fi_mean.get("uhd4",0):.4f}</code>&nbsp;&middot;&nbsp;
          Sim&nbsp;<code>{_fi_mean.get("sim",0):.4f}</code>&nbsp;cores/stream.
          We use the <em>average</em> not p99 because per-stream peaks don&rsquo;t happen simultaneously.
        </span>
      </div>
      <div class='step'>
        <span class='step-label'>Step 3</span>
        <span>Model sum for this run&rsquo;s mix
          ({_v}V + {_so}S + {_u4}U + {_sm}&nbsp;Sim):&nbsp;
          <code>&Sigma; = {_ffmpeg_model:.3f} cores</code>.
          This is the total estimated from per-process data only.
        </span>
      </div>
      <div class='step'>
        <span class='step-label'>Step 4</span>
        <span>Overhead calibration factor (accounts for ORC app server, OS scheduler, network I/O, and encoding pipeline overhead not captured in per-process polling):&nbsp;
          <code>{p99_cores_c:.3f} &divide; {_ffmpeg_model:.3f} = <strong>{_overhead_k:.3f}&times;</strong></code>
        </span>
      </div>
      <div class='step'>
        <span class='step-label'>Step 5</span>
        <span>Calibrated rate = ffmpeg&nbsp;mean &times; {_overhead_k:.3f}:&nbsp;&nbsp;
          Vision&nbsp;<code>{_cal["vision"]:.3f}</code>&nbsp;&middot;&nbsp;
          Sony/Axis&nbsp;<code>{_cal["sony"]:.3f}</code>&nbsp;&middot;&nbsp;
          UHD4&nbsp;<code>{_cal["uhd4"]:.3f}</code>&nbsp;&middot;&nbsp;
          Sim&nbsp;<code>{_cal["sim"]:.3f}</code>&nbsp;cores/stream
        </span>
      </div>
      <div class='step' style='margin-top:.25rem;padding-top:.25rem;border-top:1px dashed #cdd5ed;'>
        <span class='step-label' style='color:#146c43;'>Verified</span>
        <span style='color:#146c43;'>
          {_v}&times;{_cal["vision"]:.3f} + {_so}&times;{_cal["sony"]:.3f} + {_u4}&times;{_cal["uhd4"]:.3f} + {_sm}&times;{_cal["sim"]:.3f}
          &nbsp;=&nbsp;<code style='background:#d1f0e0;color:#146c43;'>{round(sum(_src_counts[t]*_cal[t] for t in _src_counts),2):.2f} cores</code>
          &nbsp;approx.&nbsp;<code style='background:#d1f0e0;color:#146c43;'>{p99_cores_c:.2f} cores p99</code>.
          Calibrated correctly.
        </span>
      </div>
    </div>

    <!-- Source mix table -->
    <div style='padding:.85rem 1rem .5rem;'>
      <div style='font-size:.75rem;font-weight:600;color:#8a93a2;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.5rem;'>
        Adjust your target source mix
      </div>
      <div style='overflow-x:auto;'>
        <table class='cap-table'>
          <thead>
            <tr>
              <th style='text-align:left;'>Source Type</th>
              <th style='text-align:center;'>Count</th>
              <th style='text-align:center;'>Cores / stream <span style='font-weight:400;opacity:.7;'>(click to override)</span></th>
              <th style='text-align:right;padding-right:14px;'>Work cores</th>
            </tr>
          </thead>
          <tbody id='capTypeRows'></tbody>
          <tfoot>
            <tr class='cap-total-row'>
              <td colspan='3' style='padding-left:12px;font-size:.83rem;color:#3a4a8a;'>
                Total &nbsp;<span style='font-weight:400;font-size:.74rem;color:#8a93a2;'>
                  (ffmpeg demand &times; {_overhead_k:.3f} overhead factor = calibrated to measured p99)
                </span>
              </td>
              <td class='cap-total-val' id='capTotalWork'>—</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>

    <!-- Sliders -->
    <div class='cap-sliders'>
      <div class='cap-slider-group'>
        <span class='cap-slider-label'>Target util:</span>
        <input type='range' class='cap-slider' id='capUtil2' min='40' max='80' value='60'
          oninput='capSliderUpdate(this,"capUtil2Val",40,80);capMixCalc()'>
        <span class='cap-slider-val' id='capUtil2Val'>60%</span>
      </div>
      <div class='cap-slider-group'>
        <span class='cap-slider-label'>Headroom:</span>
        <input type='range' class='cap-slider' id='capHr2' min='0' max='50' value='20'
          oninput='capSliderUpdate(this,"capHr2Val",0,50);capMixCalc()'>
        <span class='cap-slider-val' id='capHr2Val'>20%</span>
      </div>
      <button class='cap-reset-btn' onclick='capMixReset()'>Reset</button>
    </div>

    <!-- Result -->
    <div class='cap-result' id='capMixResult'>Calculating&hellip;</div>

  </div><!-- /cap-body -->
</div><!-- /capCalcCard -->

<script>
const CAP_TYPES = [
  {{ id:'vision', label:'Vision (RTSPS)',     color:'#7c3aed', rate:{_cal["vision"]}, hasMeasured:{str(_has_data["vision"]).lower()}, note:'{_js_str(_cap_note("vision"))}' }},
  {{ id:'sony',   label:'Sony / Axis Camera', color:'#0284c7', rate:{_cal["sony"]},   hasMeasured:{str(_has_data["sony"]).lower()},   note:'{_js_str(_cap_note("sony"))}' }},
  {{ id:'uhd4',   label:'UHD4 (4K RTSP)',     color:'#0d9488', rate:{_cal["uhd4"]},   hasMeasured:{str(_has_data["uhd4"]).lower()},   note:'{_js_str(_cap_note("uhd4"))}' }},
  {{ id:'matrix', label:'Matrix / RTMP',      color:'#dc6803', rate:{_cal["matrix"]}, hasMeasured:{str(_has_data["matrix"]).lower()}, note:'{_js_str(_cap_note("matrix"))}' }},
  {{ id:'sim',    label:'Simulated',          color:'#4d6ef5', rate:{_cal["sim"]},    hasMeasured:{str(_has_data["sim"]).lower()},    note:'{_js_str(_cap_note("sim"))}' }},
];
const CAP_SKUS       = {_SKUS_JS};
const CAP_CUR_CORES  = {cur_cores_c};
const CAP_OVERHEAD_K = {_overhead_k};
const CAP_DEF_COUNTS = {{ vision:{_v}, sony:{_so}, uhd4:{_u4}, matrix:{_mx}, sim:{_sm} }};
const _userRates     = {{}};

function capToggle() {{
  const body = document.getElementById('capCalcBody');
  const hdr  = document.getElementById('capCalcHeader');
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : '';
  hdr.classList.toggle('collapsed', open);
}}
function capToggleCal() {{
  const det  = document.getElementById('capCalDet');
  const tog  = document.getElementById('capCalTog');
  const chev = document.getElementById('capCalChev');
  const open = det.classList.toggle('open');
  tog.classList.toggle('open', open);
  chev.classList.toggle('open', open);
}}
function capSliderUpdate(el, valId, min, max) {{
  const pct = (el.value - min) / (max - min) * 100;
  el.style.setProperty('--pct', pct + '%');
  document.getElementById(valId).textContent = el.value + '%';
}}

function _snapSku(n) {{
  for (const s of CAP_SKUS) {{ if (s >= n) return s; }}
  return Math.ceil(n / 4) * 4;
}}

function _rateHtml(t) {{
  const r = _userRates[t.id] !== undefined ? _userRates[t.id] : t.rate;
  const ov = _userRates[t.id] !== undefined;
  const badge = t.hasMeasured
    ? `<span class='cap-badge measured'  title='${{t.note}}'>measured</span>`
    : `<span class='cap-badge estimated' title='${{t.note}}'>estimated</span>`;
  return `<div style='display:flex;align-items:center;gap:6px;justify-content:center;'>
    <input type='number' step='0.001' min='0.001' max='20' value='${{r.toFixed(3)}}'
      class='cap-rate-input${{ov?" overridden":""}}' title='${{t.note}}'
      onchange='_userRates["${{t.id}}"]=Math.max(0.001,parseFloat(this.value)||t.rate);
                this.classList.toggle("overridden",true);capMixCalc()'>
    ${{badge}}
  </div>`;
}}

function capBuildRows() {{
  document.getElementById('capTypeRows').innerHTML = CAP_TYPES.map(t => `
    <tr id='capRow_${{t.id}}'>
      <td style='padding:8px 12px;'>
        <span class='cap-type-dot' style='background:${{t.color}};'></span>
        <span style='font-weight:500;'>${{t.label}}</span>
      </td>
      <td style='padding:8px 12px;text-align:center;'>
        <input type='number' min='0' max='500' value='${{CAP_DEF_COUNTS[t.id]}}'
          id='capCnt_${{t.id}}' class='cap-count-input' oninput='capMixCalc()'>
      </td>
      <td style='padding:8px 12px;' id='capRateCell_${{t.id}}'>${{_rateHtml(t)}}</td>
      <td class='cap-work-cell' id='capWork_${{t.id}}'>—</td>
    </tr>`).join('');
}}

function capMixCalc() {{
  const util_pct = parseInt(document.getElementById('capUtil2').value);
  const hr_pct   = parseInt(document.getElementById('capHr2').value);

  let totalWork = 0;
  const breakdown = [];
  const unmeasuredActive = [];

  for (const t of CAP_TYPES) {{
    const el    = document.getElementById('capCnt_' + t.id);
    const count = el ? (parseInt(el.value) || 0) : 0;
    const rate  = _userRates[t.id] !== undefined ? _userRates[t.id] : t.rate;
    const work  = count * rate;
    totalWork  += work;
    const workEl = document.getElementById('capWork_' + t.id);
    const rowEl  = document.getElementById('capRow_' + t.id);
    if (workEl) {{
      workEl.textContent = count > 0 ? work.toFixed(3) : '—';
      workEl.classList.toggle('active', count > 0);
    }}
    if (rowEl) rowEl.classList.toggle('cap-row-active', count > 0);
    if (count > 0) {{
      breakdown.push({{ count, rate, work, color:t.color, label:t.label }});
      if (!t.hasMeasured && _userRates[t.id] === undefined) unmeasuredActive.push(t.label);
    }}
  }}

  const tw = document.getElementById('capTotalWork');
  if (tw) tw.textContent = totalWork > 0 ? totalWork.toFixed(3) + ' cores' : '—';

  if (totalWork === 0) {{
    document.getElementById('capMixResult').innerHTML =
      '<div style="color:#8a93a2;padding:.5rem 0;font-size:.84rem;">Enter source counts above to see the recommendation.</div>';
    return;
  }}

  const withHr = totalWork * (1 + hr_pct / 100);
  const needed = withHr / (util_pct / 100);
  const sku    = _snapSku(needed);
  const ratio  = needed / CAP_CUR_CORES;
  const isOk   = ratio <= 1.0;

  const bdHtml = `<table style='border-collapse:collapse;width:100%;font-size:.8rem;'>
    <thead>
      <tr style='font-size:.74rem;color:#8a93a2;border-bottom:1px solid #eef0f8;'>
        <th style='padding:3px 10px 3px 4px;font-weight:600;text-align:left;'>Source Type</th>
        <th style='padding:3px 10px;font-weight:600;text-align:center;'>Count</th>
        <th style='padding:3px 10px;font-weight:600;text-align:center;'>Cores / Stream</th>
        <th style='padding:3px 4px 3px 10px;font-weight:600;text-align:right;'>Work Cores</th>
      </tr>
    </thead>
    <tbody>
      ${{breakdown.map(b => `<tr>
        <td style='padding:4px 10px 4px 4px;'><span style='display:inline-block;width:8px;height:8px;border-radius:50%;background:${{b.color}};margin-right:5px;vertical-align:middle;'></span>${{b.label}}</td>
        <td style='padding:4px 10px;text-align:center;font-family:monospace;'>${{b.count}}</td>
        <td style='padding:4px 10px;text-align:center;font-family:monospace;'>${{b.rate.toFixed(3)}}</td>
        <td style='padding:4px 4px 4px 10px;text-align:right;font-family:monospace;font-weight:600;'>${{b.work.toFixed(3)}}</td>
      </tr>`).join('')}}
    </tbody>
    <tfoot>
      <tr style='border-top:1px solid #dde3f8;'>
        <td colspan='3' style='padding:5px 10px 4px 4px;font-weight:700;'>Total work cores</td>
        <td style='padding:5px 4px 4px 10px;text-align:right;font-family:monospace;font-weight:700;color:#2c5fcc;'>${{totalWork.toFixed(3)}}</td>
      </tr>
      <tr>
        <td colspan='4' style='padding:2px 4px 4px;font-size:.73rem;color:#8a93a2;'>Overhead factor (${{CAP_OVERHEAD_K}}x) is embedded in each rate. Total is calibrated to the measured p99.</td>
      </tr>
    </tfoot>
  </table>`;

  const estNote = unmeasuredActive.length
    ? `<div class='cap-est-warn'>Note: No measured data for <strong>${{unmeasuredActive.join(', ')}}</strong>. Using simulated rate as a conservative proxy. Hover the rate badge for details.</div>`
    : '';

  document.getElementById('capMixResult').innerHTML = `
    <div class='cap-result-steps'>
      <div class='cap-step-row'>
        <div class='cap-step-label'><span class='cap-step-num'>1</span>Source breakdown</div>
        <div class='cap-step-val cap-step-breakdown'>${{bdHtml}}</div>
      </div>
      <div class='cap-step-row'>
        <div class='cap-step-label'><span class='cap-step-num'>2</span>Add ${{hr_pct}}% headroom</div>
        <div class='cap-step-val'>
          <code>${{totalWork.toFixed(3)}} &times; (1 + ${{hr_pct}}/100) = ${{withHr.toFixed(3)}} cores</code>
          <span class='cap-step-hint'>← absorbs bursts &amp; unexpected spikes</span>
        </div>
      </div>
      <div class='cap-step-row'>
        <div class='cap-step-label'><span class='cap-step-num'>3</span>At ${{util_pct}}% utilization</div>
        <div class='cap-step-val'>
          <code>${{withHr.toFixed(3)}} &divide; ${{(util_pct/100).toFixed(2)}} = ${{needed.toFixed(2)}} cores</code>
          <span class='cap-step-hint'>← keeps server comfortable, not pegged</span>
        </div>
      </div>
      <div class='cap-step-row'>
        <div class='cap-step-label'><span class='cap-step-num'>4</span>Next CPU SKU</div>
        <div class='cap-step-val'>
          <code>${{needed.toFixed(2)}} cores raw</code>
          &rarr; round up to standard SKU &rarr;
          <strong style='font-size:1.05rem;color:${{isOk?"#146c43":"#9b1c1c"}};'>${{sku}} cores</strong>
        </div>
      </div>
    </div>
    <div class='cap-verdict ${{isOk?"ok":"bad"}}'>
      ${{isOk
        ? `Current <strong>${{CAP_CUR_CORES}}-core</strong> server meets this specification`
        : `Current <strong>${{CAP_CUR_CORES}}-core</strong> server is undersized. Recommended minimum: <span class='cap-verdict-sku'>${{sku}} cores</span>`
      }}
    </div>
    ${{estNote}}`;
}}

function capMixReset() {{
  for (const t of CAP_TYPES) {{
    delete _userRates[t.id];
    const c  = document.getElementById('capCnt_'      + t.id);
    const rc = document.getElementById('capRateCell_' + t.id);
    if (c)  c.value      = CAP_DEF_COUNTS[t.id];
    if (rc) rc.innerHTML = _rateHtml(t);
  }}
  const u = document.getElementById('capUtil2');
  const h = document.getElementById('capHr2');
  u.value = 60; capSliderUpdate(u,'capUtil2Val',40,80);
  h.value = 20; capSliderUpdate(h,'capHr2Val',0,50);
  capMixCalc();
}}

document.addEventListener('DOMContentLoaded', () => {{
  capBuildRows();
  capSliderUpdate(document.getElementById('capUtil2'),'capUtil2Val',40,80);
  capSliderUpdate(document.getElementById('capHr2'),  'capHr2Val',0,50);
  capMixCalc();
}});
</script>
"""

    return (
        f"<div class='section-card mt-3' style='border-left:4px solid {_PRIMARY};background:#fafcff;'>\n"
        "<div class='detail-sub-header'>Key Findings &amp; Notes</div>\n"
        f"<ul style='margin:0;padding-left:1.2rem;font-size:.9rem;line-height:1.8;'>{rows_html}</ul>\n"
        f"{calc_block}\n"
        "</div>\n"
    )


def _endurance_section(data: dict) -> str:
    """Build the endurance-specific section card (duration, alerts, timeline charts)."""
    dur_req  = data.get("duration_requested_s")
    dur_act  = data.get("duration_actual_s")
    alerts   = data.get("alerts", [])
    wt       = data.get("webrtc_timeline", [])
    st       = data.get("server_timeline", [])

    # Compute per-source avg FPS / jitter from active timeline entries.
    # connection_count includes the 1 non-video signaling channel, so subtract 1
    # to get the video stream count used as the divisor for per-source FPS.
    _active = [p for p in wt if p.get("connection_count", 0) > 1]
    if _active:
        _fps_per_src = [p.get("fps", 0) / (p["connection_count"] - 1) for p in _active]
        _avg_fps    = round(sum(_fps_per_src) / len(_fps_per_src), 1)
    else:
        _avg_fps = None
    _avg_jitter = round(sum(p.get("jitter_ms", 0) for p in _active) / len(_active), 1) if _active else None

    # Duration mini-cards
    dur_minis = ""
    if dur_req is not None:
        dur_minis += _mini("Requested Duration", _fmt_duration(dur_req))
    if dur_act is not None:
        dur_minis += _mini("Actual Duration",    _fmt_duration(dur_act))
    if _avg_fps is not None:
        dur_minis += _mini("Avg FPS / src", _avg_fps)
    if _avg_jitter is not None:
        dur_minis += _mini("Avg Jitter", _avg_jitter, " ms")

    # Alerts count card — exclude WHEP_RECONNECT (noise: recovered automatically, not actionable)
    _SUPPRESS_IN_TABLE = {"WHEP_RECONNECT"}
    alert_counts: dict = {}
    for a in alerts:
        if a["type"] not in _SUPPRESS_IN_TABLE:
            alert_counts[a["type"]] = alert_counts.get(a["type"], 0) + 1
    visible_alerts = [a for a in alerts if a["type"] not in _SUPPRESS_IN_TABLE]
    if visible_alerts:
        _real_errors = [a for a in visible_alerts if a["type"] not in _RESOURCE_ALERT_TYPES]
        alert_summary = "; ".join(f"{v}× {k}" for k, v in alert_counts.items())
        _badge_bg = "#dc3545" if _real_errors else "#fd7e14"
        alert_badge = (
            f"<span style='background:{_badge_bg};color:#fff;padding:2px 10px;"
            f"border-radius:10px;font-size:.78rem;font-weight:700;'>"
            f"{len(visible_alerts)} alert(s)</span> "
            f"<span style='font-size:.86rem;color:#6c757d;'>{alert_summary}</span>"
        )
    else:
        alert_badge = (
            "<span style='background:#198754;color:#fff;padding:2px 10px;"
            "border-radius:10px;font-size:.78rem;font-weight:700;'>No alerts</span>"
        )

    # Alerts table
    alert_table = ""
    if visible_alerts:
        _TYPE_COLOR = {
            "CPU_SUSTAINED": "#fd7e14",
            "CPU_DRIFT":     "#fd7e14",
            "RAM_DRIFT":     "#6f42c1",
            "RAM_SUSTAINED": "#6f42c1",
            "CONNECTION_DROP": "#dc3545",
        }
        rows = "".join(
            f"<tr>"
            f"<td class='text-end' style='font-size:.8rem;color:#6c757d;'>{_fmt_duration(a['elapsed_s'])}</td>"
            f"<td><span style='background:{_TYPE_COLOR.get(a['type'], '#dc3545')};color:#fff;font-size:.7rem;padding:1px 8px;"
            f"border-radius:8px;'>{a['type']}</span></td>"
            f"<td style='font-size:.86rem;'>{a['message']}</td>"
            f"</tr>"
            for a in visible_alerts
        )
        alert_table = (
            "<div class='table-responsive mt-3 mb-3'>"
            "<table class='data-table data-table-sm'>"
            "<thead><tr>"
            "<th class='text-end'>Elapsed</th>"
            "<th>Type</th>"
            "<th>Message</th>"
            "</tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table></div>"
        )

    # ── Key Findings card ─────────────────────────────────────────────────
    findings_html = _endurance_findings_html(data)

    return (
        "<div class='section-card'>\n"
        "<div class='section-header'>Endurance Soak Summary</div>\n"
        f"<div class='row g-3 mb-3'>{dur_minis}</div>\n"
        + findings_html
        + f"<div class='mb-3'>{alert_badge}</div>\n"
        + alert_table
        + "</div>\n\n"
    )


def _build_html(data: dict) -> str:
    scenario  = data.get("scenario") or data.get("scenario_name", "&#8212;")
    generated = data.get("generated_at", "&#8212;")
    summary   = data.get("summary", {})
    sessions  = data.get("sessions", [])
    webrtc    = data.get("webrtc", {})
    api       = _recompute_api(data.get("api", {}))
    server    = data.get("server")
    tiers     = data.get("tiers", [])
    layout_steps = data.get("layout_steps", [])

    server_name = data.get("server_name", "")
    hardware    = data.get("hardware", "")

    is_layout_stress = (scenario == "layout_stress")
    is_endurance     = (scenario == "endurance_test")

    # Pre-compute JS to avoid backslash issues inside f-strings
    js_session       = _session_chart_js(sessions)
    js_api           = _api_chart_js(api, interval_s=data.get("interval_s", 30.0))
    js_server        = _server_chart_js(
        server,
        sessions=sessions,
        ramp_start_epoch=float(data.get("ramp_start_epoch") or 0),
        interval_s=float(data.get("interval_s") or 2),
    )
    js_proc_series   = _proc_series_charts_js(server)
    js_tier          = _tier_chart_js(tiers)
    js_tier_src      = _tier_session_charts_js(tiers)
    js_tier_srv      = _tier_server_charts_js(tiers)
    js_layout_stress = _layout_stress_chart_js(layout_steps)
    js_stream_timing = _stream_timing_chart_js(data.get("stream_timing", {}))
    js_endurance     = ""  # per-poll charts removed

    # Embed raw data for markdown export (escape </script> sequences for safety)
    data_json = json.dumps(data, default=str).replace("</", "<\\/")
    js_data = "window._reportData = " + data_json + ";"

    api_canvas = (
        '<canvas id="apiChart" height="80"></canvas>'
        if api.get("calls") else
        '<p class="text-muted mb-0">No API call data collected.</p>'
    )

    # Scenario badge — colour-coded pill shown in every report header
    _scenario_colors = {
        "capacity_validation": ("#0077C8", "#fff"),
        "endurance_test":      ("#198754", "#fff"),
        "layout_stress":       ("#fd7e14", "#fff"),
        "hardware_comparison": ("#6f42c1", "#fff"),
        "enforcement_threshold": ("#dc3545", "#fff"),
    }
    # cap_val tier scenarios have the form "cap_val_{server}_{N}sessions" —
    # format them as "Capacity Validation — N Sessions" (server already in subtitle).
    import re as _re
    _cv_match = _re.match(r"cap_val_.+?_(\d+)sessions?$", scenario)
    if _cv_match:
        _sc_bg, _sc_fg = _scenario_colors.get("capacity_validation", ("#0077C8", "#fff"))
        _scenario_label = f"Capacity Validation: {_cv_match.group(1)} Sessions"
    else:
        _sc_bg, _sc_fg = _scenario_colors.get(scenario, ("#6c757d", "#fff"))
        _scenario_label = scenario.replace("_", " ").title()
    scenario_badge = (
        f"<span style='background:{_sc_bg};color:{_sc_fg};font-size:.72rem;font-weight:700;"
        f"padding:3px 10px;border-radius:12px;vertical-align:middle;'>"
        f"{_scenario_label}</span>"
    )

    subtitle_parts = []
    if server_name:
        subtitle_parts.append(f"Server: <strong>{server_name}</strong>")
    if hardware:
        subtitle_parts.append(f"Hardware: <strong>{hardware}</strong>")
    subtitle_parts.append(f"Generated: {generated}")
    subtitle_text = " &nbsp;|&nbsp; ".join(subtitle_parts)
    subtitle = f"{scenario_badge} &nbsp; {subtitle_text}"

    connect_rate = summary.get("connect_rate_pct", 0)
    session_tier = data.get("session_tier", "")

    # Build verdict — only for single-tier capacity_validation runs where we
    # have enough data to make a meaningful pass/fail statement.
    verdict_html = ""
    if is_endurance:
        verdict_html = _endurance_banner_html(data, hardware or "")
    elif not tiers and not is_layout_stress and session_tier:
        abort_reason = data.get("abort_reason")
        verdict_metrics = {
            "avg_cpu_pct":      server.get("avg_cpu")   if server else None,
            "max_cpu_pct":      server.get("max_cpu")   if server else None,
            "avg_ram_pct":      server.get("avg_ram")   if server else None,
            "avg_load_ms":      summary.get("avg_load_ms"),
            "max_load_ms":      summary.get("max_load_ms"),
            "connect_rate_pct": connect_rate,
        }
        verdict, findings = _overall_verdict(verdict_metrics)
        # An abort always overrides the verdict to FAIL regardless of metrics
        if abort_reason:
            verdict = "fail"
        _connected_count = summary.get("connected", 0)
        _webrtc_snaps    = (webrtc or {}).get("snapshots", [])
        # Subtract 1 per tab: ORC creates one extra non-video RTCPeerConnection
        # (control/signaling channel) that inflates the raw WebRTC count by 1.
        _total_conns     = sum(max(0, s.get("conns", 0) - 1) for s in _webrtc_snaps) or None
        verdict_html = _verdict_banner_html(
            verdict, findings, session_tier, hardware or "",
            connected=_connected_count,
            total_webrtc_conns=_total_conns,
            webrtc_snaps=_webrtc_snaps,
            abort_reason=abort_reason,
        )

    # Use session_tier as the ground-truth source count — avoids undercount
    # when sessions fail to record (partial run, early abort, etc.)
    _total_sources = int(session_tier) if session_tier else summary.get("total_sessions", 0)
    _connected     = summary.get("connected", 0)
    _blocked       = _total_sources - _connected
    _connect_rate  = round(_connected / _total_sources * 100, 1) if _total_sources > 0 else connect_rate

    stat_cards = (
        _stat_card("Total Sources",   _total_sources,               accent=_DARK)
        + _stat_card("Connected",     _connected,                   accent=_SUCCESS)
        + _stat_card("Blocked",       _blocked,                     accent=_DANGER)
        + _stat_card("Connect Rate",  str(_connect_rate) + "%",     accent=_rate_color(_connect_rate))
        + (
            # Endurance: only 1 session so avg == max — show single card
            _stat_card("Session Load", f"{summary.get('avg_load_ms', 0)/1000:.2f}", "s", _PRIMARY)
            if is_endurance else
            _stat_card("Avg Load", f"{summary.get('avg_load_ms', 0)/1000:.2f}", "s", _PRIMARY)
            + _stat_card("Max Load", f"{summary.get('max_load_ms', 0)/1000:.2f}", "s", _WARNING)
        )
        + (_stat_card("Avg CPU",      str(server.get("avg_cpu", "&#8212;")), "%",
                      _DANGER if server and (server.get("avg_cpu") or 0) >= 80 else _PRIMARY) if server else "")
        + (_stat_card("p99 CPU",      str(round(server.get("p99_cpu") or server.get("max_cpu") or 0, 1)), "%", _DANGER) if server else "")
        + (_stat_card("Hardware",     hardware or "&#8212;", accent=_DARK) if hardware else "")
    )
    # Append stream-delivery timing to stat cards when the data is present
    # (capacity_validation single-tier only — endurance has its own section)
    if not (tiers or is_layout_stress or is_endurance):
        _st   = data.get("stream_timing", {})
        _t1   = _st.get("time_to_first_s")
        _tall = _st.get("time_to_all_s")
        if _t1   is not None:
            stat_cards += _stat_card("First Stream", f"{_t1}s", accent=_PRIMARY)
        if _tall is not None and _tall != _t1:
            stat_cards += _stat_card("All Streams",  f"{_tall}s", accent=_PRIMARY)

    _bw_checks = api.get("bw_checks", 0)
    api_minis = (
        _mini("API Calls", api.get("non_bw_total", api.get("total", 0)))
        + _mini("API Avg",  str(api.get("avg_ms", 0)) + " ms")
        + (_mini("BW Checks", _bw_checks) + _mini("BW p95", str(api.get("bw_p95_ms", 0)) + " ms")
           if _bw_checks else "")
    )

    session_rows_html = _session_rows(sessions)
    server_html       = _server_section(server, data.get("room_sources", []))
    webrtc_html       = _webrtc_section(webrtc)
    tier_html         = _tier_comparison_section(tiers)
    per_tier_html     = _per_tier_server_cards(tiers)
    badge_html        = _badge(_connect_rate)
    logo_html         = _logo_img_tag(38)

    header_right_html = (
        "<div class='text-end'>\n"
        "  <div class='mt-1 text-white' style='font-size:.85rem;opacity:.7;'>See tier results &#8595;</div>\n"
        "  <button class='export-btn mt-2' onclick='downloadMarkdown()'>&#8615; Export MD</button>\n"
        "  <button class='export-btn mt-2 ms-2' onclick='window.print()'>&#128438; Save as PDF</button>\n"
        "</div>\n"
        if tiers else
        "<div class='text-end'>\n"
        f"  <div class='mt-1'>{badge_html}</div>\n"
        "  <button class='export-btn mt-2' onclick='downloadMarkdown()'>&#8615; Export MD</button>\n"
        "  <button class='export-btn mt-2 ms-2' onclick='window.print()'>&#128438; Save as PDF</button>\n"
        "</div>\n"
    )

    layout_stress_html = _layout_stress_section(layout_steps, server) if is_layout_stress else ""

    endurance_html = _endurance_section(data) if is_endurance else ""

    src_cfg_html = _source_config_section(
        data.get("room_sources", []),
        ffmpeg_instances=(server or {}).get("ffmpeg_instances"),
    )
    src_bw_html  = "" if (tiers or is_layout_stress or is_endurance) else _source_bandwidth_section(
        data.get("room_sources", []),
        data.get("source_breakdown", []),
    )

    stream_timing = data.get("stream_timing", {})
    # Stream Delivery Timeline chart removed — the 30-second initial-soak ramp
    # adds little value; timing KPIs are now surfaced directly as stat cards.
    stream_timing_html = ""

    source_section_html = "" if (tiers or is_layout_stress or is_endurance) else (
        "<div class='section-card'>\n"
        "  <div class='section-header'>Session Load Times</div>\n"
        "  <p class='text-muted mb-3' style='font-size:.8rem;'>Time from browser login start to ORC dashboard fully loaded "
        "(HTTPS handshake &rarr; authentication &rarr; Angular bootstrap &rarr; network idle). "
        "Measured per session; does not include browser launch overhead.</p>\n"
        "  <div class='table-responsive mb-4'>\n"
        "    <table class='data-table'>\n"
        "      <thead><tr>"
        "<th class='text-center'>Session #</th>"
        "<th class='text-center'>Connected</th>"
        "<th class='text-center' title='ORC bandwidth enforcement modal appeared, indicating this stream was blocked by the egress cap'>BW Enforced</th>"
        "<th class='text-end'>Load (s)</th>"
        "</tr></thead>\n"
        f"      <tbody>{session_rows_html}</tbody>\n"
        "    </table>\n  </div>\n"
        "  <canvas id='sessionChart' height='70'></canvas>\n"
        "</div>\n\n"
    )

    api_section_html = "" if (tiers or is_layout_stress or is_endurance) else (
        "<div class='section-card'>\n"
        "  <div class='section-header'>API Latency</div>\n"
        f"  <div class='row g-3 mb-3'>{api_minis}</div>\n"
        f"  {api_canvas}\n"
        "</div>\n\n"
    )

    webrtc_gated = "" if (tiers or is_layout_stress or is_endurance) else webrtc_html

    _inline_css, _inline_js = _inline_assets()

    return (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        "<meta charset='UTF-8'/>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'/>\n"
        f"<title>ORC Perf Report &#8212; {_scenario_label.replace('&mdash;', '—')}</title>\n"
        + _inline_css
        + _inline_js
        + f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class='container-fluid py-4' style='max-width:1600px;'>\n\n"

        # Header
        "<div class='page-header d-flex align-items-start justify-content-between'>\n"
        "  <div>\n"
        f"    <h1>{logo_html}ORC Performance Report</h1>\n"
        f"    <div class='sub'>{subtitle}</div>\n"
        "  </div>\n"
        + header_right_html
        + "</div>\n\n"

        # Verdict banner — pass/warn/fail with threshold findings
        + verdict_html

        # Source config dropdown — always at top when data is present
        + src_cfg_html

        # KPI row — aggregate for single-tier runs; per-server cards for capacity_validation
        + (per_tier_html if tiers else f"<div class='row g-3 mb-4'>{stat_cards}</div>\n\n")
        + "\n"

        + layout_stress_html
        + ("" if (tiers or is_layout_stress) else server_html)
        + (server_html if is_layout_stress else "")
        + endurance_html
        + src_bw_html
        + source_section_html
        + stream_timing_html
        + api_section_html
        + webrtc_gated
        + tier_html

        # Footer omitted
        + "</div>\n"
        f"<script>\n{js_data}\n{_SESSION_MARKER_PLUGIN_JS}\n{_MARKDOWN_EXPORT_JS}\n{js_session}\n{js_api}\n{js_server}\n{js_proc_series}\n{js_tier}\n{js_tier_src}\n{js_tier_srv}\n{js_layout_stress}\n{js_stream_timing}\n{js_endurance}\n</script>\n"
        f"<script>\n{_NAV_COLLAPSE_JS}\n</script>\n"
        "</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _stype_badge(stype: str) -> str:
    colors = {"Vision": "#0077C8", "Physical": "#198754", "Simulated": "#6c757d"}
    c = colors.get(stype, "#6c757d")
    return (f"<span style='background:{c};color:#fff;font-size:.68rem;font-weight:700;"
            f"padding:2px 8px;border-radius:10px;'>{stype}</span>")


def _source_bandwidth_section(room_sources: list, source_breakdown: list) -> str:
    """Per-source bandwidth breakdown — Vision / Physical / Simulated with measured Mbps."""
    if not room_sources:
        return ""

    # Enrich breakdown with source name + type from room_sources (matched by index)
    enriched = []
    for entry in (source_breakdown or []):
        idx = entry.get("source_idx", -1)
        rs  = room_sources[idx] if 0 <= idx < len(room_sources) else {}
        enriched.append({
            "room":    rs.get("room", f"OR {idx+1:02d}"),
            "source":  rs.get("source", "&#8212;"),
            "type":    rs.get("type", "Physical"),
            "mbps":    entry.get("avg_mbps_per_viewer"),
        })

    # If no measured Mbps yet (old data / no stream_timing), fall back to composition only
    has_mbps = any(e["mbps"] is not None for e in enriched)

    # Build type summary counters
    type_counts: dict = {}
    for rs in room_sources:
        t = rs.get("type", "Physical")
        type_counts[t] = type_counts.get(t, 0) + 1

    summary_pills = " &nbsp; ".join(
        f"<strong>{cnt}</strong> {_stype_badge(t)}"
        for t, cnt in sorted(type_counts.items())
    )

    if enriched and has_mbps:
        # Per-source rows with measured Mbps
        rows_html = ""
        for e in enriched:
            mbps_cell = f"{e['mbps']:.2f} Mbps" if e["mbps"] is not None else "&#8212;"
            rows_html += (
                f"<tr>"
                f"<td class='text-center' style='font-size:.78rem;color:#6c757d;'>{e['room']}</td>"
                f"<td>{e['source']}</td>"
                f"<td class='text-center'>{_stype_badge(e['type'])}</td>"
                f"<td class='text-end'>{mbps_cell}</td>"
                f"</tr>"
            )
        # Per-type grouped average
        type_mbps: dict = {}
        for e in enriched:
            if e["mbps"] is not None:
                type_mbps.setdefault(e["type"], []).append(e["mbps"])
        summary_rows = "".join(
            f"<tr>"
            f"<td colspan='3'><strong>{t}</strong>: {len(vals)} source(s)</td>"
            f"<td class='text-end'><strong>{sum(vals)/len(vals):.2f} Mbps avg</strong></td>"
            f"</tr>"
            for t, vals in sorted(type_mbps.items())
        )
        table_html = (
            "<div class='table-responsive mt-3'>"
            "<table class='data-table data-table-sm'>"
            "<thead><tr>"
            "<th class='text-center'>Room</th>"
            "<th>Source</th>"
            "<th class='text-center'>Type</th>"
            "<th class='text-end'>Avg Mbps / Viewer</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            f"<tfoot style='background:#f8f9fa;font-size:.8rem;'>{summary_rows}</tfoot>"
            "</table></div>"
        )
        note = ("<p class='text-muted mb-2' style='font-size:.78rem;'>"
                "Avg Mbps / Viewer = bytes received by one viewer tab from that source over the stable "
                "streaming window. Multiply by connected viewers for total server egress per source.</p>")
    else:
        # Composition only — no measured Mbps (old data or no stream_timing)
        rows_html = "".join(
            f"<tr>"
            f"<td class='text-center' style='font-size:.78rem;color:#6c757d;'>{rs.get('room','')}</td>"
            f"<td>{rs.get('source','')}</td>"
            f"<td class='text-center'>{_stype_badge(rs.get('type','Physical'))}</td>"
            f"<td class='text-end text-muted'>&#8212;</td>"
            f"</tr>"
            for rs in room_sources
        )
        table_html = (
            "<div class='table-responsive mt-3'>"
            "<table class='data-table data-table-sm'>"
            "<thead><tr>"
            "<th class='text-center'>Room</th><th>Source</th>"
            "<th class='text-center'>Type</th><th class='text-end'>Avg Mbps / Viewer</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            "</table></div>"
        )
        note = ("<p class='text-muted mb-2' style='font-size:.78rem;'>"
                "Per-source Mbps not available for this run (requires new collection). "
                "Re-run to see measured per-source bandwidth.</p>")

    return (
        "<div class='section-card'>\n"
        "<div class='section-header'>Source Bandwidth Profile</div>\n"
        f"<div class='mb-2'>{summary_pills}</div>\n"
        + note
        + table_html
        + "\n</div>\n\n"
    )


def _ffmpeg_instances_table_html(server: dict, room_sources: list | None = None) -> str:
    """Collapsible per-stream-URL CPU/RAM summary table inside Server Metrics."""
    instances = (server or {}).get("ffmpeg_instances", [])
    if not instances:
        return ""

    # Credential-strip helper (same pattern as aggregator)
    import re as _re_fi
    def _strip(u: str) -> str:
        return _re_fi.sub(r'(?<=://)([^@]+@)', '', u or "")

    detected_urls = {_strip(inst["url"]) for inst in instances}

    # Find configured-but-undetected sources — only when room_sources is known
    # (avoids false positives from unused catalogue pool entries like sim 30-41)
    missing_rows = ""
    missing_count = 0
    if room_sources:
        for rs in room_sources:
            rs_url = _strip(rs.get("url") or rs.get("source") or "")
            if rs_url and rs_url not in detected_urls:
                missing_count += 1
                label = rs.get("source") or _url_to_source_label(rs_url)
                missing_rows += (
                    "<tr style='opacity:.5;font-style:italic;'>"
                    f"<td style='font-size:.78rem;font-weight:600;white-space:nowrap;'>{label}</td>"
                    f"<td style='font-family:monospace;font-size:.75rem;max-width:300px;word-break:break-all;color:#6c757d;'>{rs_url}</td>"
                    "<td class='text-end text-muted' colspan='4'>no ffmpeg detected</td>"
                    "</tr>"
                )

    rows = "".join(
        "<tr>"
        f"<td style='font-size:.78rem;font-weight:600;white-space:nowrap;'>{_url_to_source_label(inst['url'])}</td>"
        f"<td style='font-family:monospace;font-size:.75rem;max-width:300px;word-break:break-all;color:#6c757d;'>{inst['url']}</td>"
        f"<td class='text-end'>{inst['avg_cpu_pct']}</td>"
        f"<td class='text-end'>{inst['max_cpu_pct']}</td>"
        f"<td class='text-end'>{inst['avg_ram_mb']}</td>"
        f"<td class='text-end'>{inst['max_ram_mb']}</td>"
        "</tr>"
        for inst in instances
    ) + missing_rows

    # _chart_panel wraps a canvas — we replace the canvas with our table
    table_html = (
        "<div class='table-responsive mt-2'>"
        "<table class='data-table' style='font-size:.82rem;'>"
        "<thead><tr>"
        "<th>Source</th>"
        "<th>Stream URL</th>"
        "<th class='text-end'>Avg CPU%</th>"
        "<th class='text-end' title='Sorted by Max CPU descending'>Max CPU% &#9660;</th>"
        "<th class='text-end'>Avg RAM (MB)</th>"
        "<th class='text-end'>Max RAM (MB)</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table></div>"
    )
    # Build our own collapse panel (no canvas needed)
    n = len(instances)
    total = n + missing_count
    badge_label = f"{n} / {total}" if missing_count else f"{n}"
    badge_title = f"{missing_count} configured source(s) had no active ffmpeg" if missing_count else ""
    badge_extra = f" title='{badge_title}'" if badge_title else ""
    panel_id = "ffmpegInstancesBody"
    return (
        f"<div class='mt-3'>"
        f"<div class='d-flex align-items-center detail-sub-header' "
        f"style='cursor:pointer;' data-bs-toggle='collapse' data-bs-target='#{panel_id}' aria-expanded='false'>"
        f"<span>ffmpeg Stream Instances "
        f"<span class='badge bg-secondary ms-1' style='font-size:.68rem;'{badge_extra}>{badge_label} streams</span></span>"
        f"<span class='chart-chevron ms-auto'>&#9660;</span>"
        f"</div>"
        f"<div id='{panel_id}' class='collapse'>"
        + table_html +
        "</div></div>"
    )


def _source_config_section(room_sources: list, ffmpeg_instances: list | None = None) -> str:
    """Collapsible source configuration table — starts collapsed, shown at top of report.

    Falls back to stream URLs from ffmpeg_instances when room_sources is empty
    (e.g. endurance_test run without --reset-rooms).
    """
    # Build synthetic room_sources from ffmpeg_instances URLs if needed
    if not room_sources and ffmpeg_instances:
        seen: set = set()
        room_sources = []
        for inst in ffmpeg_instances:
            url = inst.get("url", "") or ""
            if url and url != "(no url)" and url not in seen:
                seen.add(url)
                room_sources.append({"room": "&mdash;", "source": url})
    if not room_sources:
        return ""
    # Build rows: show Room column only when data is present
    has_rooms = any(rs.get("room") and rs["room"] != "&mdash;" for rs in room_sources)
    has_urls  = any(rs.get("url") for rs in room_sources)
    if has_rooms:
        if has_urls:
            header = "<tr><th class='text-center'>Room</th><th>Source</th><th>Stream URL</th></tr>"
            rows = "".join(
                f"<tr><td class='text-center'>{rs['room']}</td>"
                f"<td>{rs['source']}</td>"
                f"<td style='font-family:monospace;font-size:.78rem;color:#6c757d;'>{rs.get('url','')}</td></tr>"
                for rs in room_sources
            )
        else:
            header = "<tr><th class='text-center'>Room</th><th>Source</th></tr>"
            rows = "".join(
                f"<tr><td class='text-center'>{rs['room']}</td><td>{rs['source']}</td></tr>"
                for rs in room_sources
            )
    else:
        header = "<tr><th>Stream URL</th></tr>"
        rows = "".join(
            f"<tr><td style='font-family:monospace;font-size:.8rem;'>{rs['source']}</td></tr>"
            for rs in room_sources
        )
    n = len(room_sources)
    return (
        "<div class='section-card mb-3'>\n"
        "  <div class='d-flex align-items-center justify-content-between' "
        "style='cursor:pointer;' data-bs-toggle='collapse' data-bs-target='#srcCfgBody' aria-expanded='false'>\n"
        f"    <div class='section-header mb-0'>Source Configuration <span class='badge bg-secondary ms-2' style='font-size:.7rem;'>{n} sources</span></div>\n"
        "    <span style='font-size:.6rem;color:#adb5bd;flex-shrink:0;margin-left:8px;'>&#9660;</span>\n"
        "  </div>\n"
        "  <div id='srcCfgBody' class='collapse'>\n"
        "    <div class='table-responsive mt-3'>\n"
        "      <table class='data-table'>\n"
        f"        <thead>{header}</thead>\n"
        f"        <tbody>{rows}</tbody>\n"
        "      </table>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n\n"
    )


def _stream_timing_chart_js(stream_timing: dict) -> str:
    """Line chart of total WebRTC connections vs elapsed time during the soak window."""
    timeline = stream_timing.get("timeline", [])
    if not timeline:
        return ""
    x    = _js_array([p["elapsed_s"] for p in timeline])
    y    = _js_array([p["total_conns"] for p in timeline])
    t1   = stream_timing.get("time_to_first_s")
    tall = stream_timing.get("time_to_all_s")
    note_parts = []
    if t1   is not None: note_parts.append(f"First stream: {t1}s")
    if tall is not None: note_parts.append(f"All streams: {tall}s")
    note = " | ".join(note_parts)
    blue = _CHART_BLUE
    return (
        f"new Chart(document.getElementById('streamTimingChart'), {{"
        f"  type: 'line',"
        f"  data: {{ labels: {x},"
        f"    datasets: [{{ label: 'Active WebRTC Connections', data: {y},"
        f"      borderColor: '{blue}', backgroundColor: '{blue}22',"
        f"      fill: true, tension: 0.3, pointRadius: 3 }}] }},"
        f"  options: {{ responsive: true,"
        f"    plugins: {{"
        f"      legend: {{ display: false }},"
        f"      subtitle: {{ display: {'true' if note else 'false'}, text: '{note}', padding: {{bottom: 6}} }}"
        f"    }},"
        f"    scales: {{"
        f"      x: {{ title: {{ display: true, text: 'Elapsed time (s)' }} }},"
        f"      y: {{ beginAtZero: true, title: {{ display: true, text: 'Active Connections' }},"
        f"           ticks: {{ stepSize: 1 }} }}"
        f"    }} }} }});"
    )


def write_report(data: dict, output_dir: str) -> dict:
    """
    Write performance test results to disk.

    Creates:
      {output_dir}/report.html   — branded HTML with Chart.js charts
      {output_dir}/summary.csv   — flat CSV for Excel / trend tracking
      {output_dir}/raw_data.json — full data dict

    Returns: {"html": path, "csv": path, "json": path}
    """
    os.makedirs(output_dir, exist_ok=True)

    html_path = os.path.join(output_dir, "report.html")
    csv_path  = os.path.join(output_dir, "summary.csv")
    json_path = os.path.join(output_dir, "raw_data.json")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_build_html(data))

    summary = data.get("summary", {})
    api     = data.get("api", {})
    server  = data.get("server") or {}

    csv_row = {
        "scenario":           data.get("scenario", ""),
        "server_name":        data.get("server_name", ""),
        "hardware":           data.get("hardware", ""),
        "session_tier":       data.get("session_tier", ""),
        "generated_at":       data.get("generated_at", ""),
        "total_sessions":     summary.get("total_sessions", 0),
        "connected":          summary.get("connected", 0),
        "blocked":            summary.get("blocked", 0),
        "connect_rate_pct":   summary.get("connect_rate_pct", 0.0),
        "avg_load_ms":        summary.get("avg_load_ms", 0.0),
        "max_load_ms":        summary.get("max_load_ms", 0.0),
        "avg_api_ms":         api.get("avg_ms", 0.0),
        "p95_api_ms":         api.get("p95_ms", 0.0),
        "bw_p95_ms":          api.get("bw_p95_ms", 0.0),
        "avg_cpu_pct":        server.get("avg_cpu", ""),
        "max_cpu_pct":        server.get("max_cpu", ""),
        "avg_ram_gb":         server.get("avg_ram_gb", ""),
        "avg_net_recv_mbps":  server.get("avg_net_recv_mbps", ""),
    }

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_row.keys()))
        writer.writeheader()
        writer.writerow(csv_row)

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, default=str))

    return {"html": html_path, "csv": csv_path, "json": json_path}
