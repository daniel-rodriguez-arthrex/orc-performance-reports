"""
generate_srs_handle_report.py
==============================
Generate a branded HTML report from srs_handles.csv collected during
an SRS handle-leak endurance test.  Uses the same CSS, logo, Bootstrap
and Chart.js stack as the existing ORC performance reports.

Usage:
    python generate_srs_handle_report.py [results_dir]

    results_dir must contain srs_handles.csv.
    Writes report.html into the same directory.

Example:
    python generate_srs_handle_report.py results/run_2026-06-04_srs_handle_leak_168h_qa172_01d94e
"""

import csv
import json
import os
import statistics
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Reuse branding + CSS from the existing report module
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
from reporting.report import _CSS, _logo_img_tag, _inline_assets, _stat_card  # noqa: E402

_PRIMARY = "#0077C8"
_DARK    = "#1a2332"
_SUCCESS = "#198754"
_WARNING = "#fd7e14"
_DANGER  = "#dc3545"
_PURPLE  = "#6f42c1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds, 3600)
    m, s = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _js_array(values: list) -> str:
    return "[" + ",".join(json.dumps(v) for v in values) + "]"


def _downsample(lst: list, max_points: int = 1440) -> list:
    if len(lst) <= max_points:
        return lst
    step = len(lst) / max_points
    return [lst[int(i * step)] for i in range(max_points)]


# ---------------------------------------------------------------------------
# CSV loading + stats
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_stats(rows: list[dict]) -> dict:
    all_handles = [int(r["handle_count"]) for r in rows]
    nz_handles  = [h for h in all_handles if h > 0]
    zero_cnt    = sum(1 for h in all_handles if h == 0)
    wset        = [float(r["working_set_mb"]) for r in rows]
    priv        = [float(r["private_mb"]) for r in rows]
    avail       = [float(r["sys_avail_mb"]) for r in rows]
    ffmpeg      = [int(r["ffmpeg_count"]) for r in rows]
    elapsed_all = [float(r["elapsed_s"]) for r in rows]

    # Detect SRS restarts: large negative jumps
    nz_rows = [(r["timestamp"], int(r["handle_count"]), float(r["elapsed_s"]))
               for r in rows if int(r["handle_count"]) > 0]
    restarts = []
    for i in range(1, len(nz_rows)):
        delta = nz_rows[i][1] - nz_rows[i - 1][1]
        if delta < -10_000:
            restarts.append({
                "timestamp": nz_rows[i][0],
                "elapsed_s": nz_rows[i][2],
                "from":      nz_rows[i - 1][1],
                "to":        nz_rows[i][1],
                "drop":      delta,
            })

    duration_s = elapsed_all[-1] if elapsed_all else 0

    # Use the post-restart baseline as "start" — that is the meaningful leak measurement
    if restarts:
        last_restart_elapsed = restarts[-1]["elapsed_s"]
        post = [(ts, h, el) for ts, h, el in nz_rows if el >= last_restart_elapsed]
    else:
        post = nz_rows

    handle_start = post[0][1]  if post else (nz_rows[0][1]  if nz_rows else 0)
    handle_end   = post[-1][1] if post else (nz_rows[-1][1] if nz_rows else 0)
    post_dur_s   = (post[-1][2] - post[0][2]) if len(post) >= 2 else duration_s
    post_rate    = (handle_end - handle_start) / (post_dur_s / 3600) if post_dur_s > 0 else 0

    return {
        "total_rows":    len(rows),
        "zero_samples":  zero_cnt,
        "first_ts":      rows[0]["timestamp"],
        "last_ts":       rows[-1]["timestamp"],
        "duration_s":    duration_s,
        "duration_h":    duration_s / 3600,
        "handle_min":    min(nz_handles) if nz_handles else 0,
        "handle_max":    max(nz_handles) if nz_handles else 0,
        "handle_avg":    round(statistics.mean(nz_handles)) if nz_handles else 0,
        "handle_start":  handle_start,   # post-restart baseline
        "handle_end":    handle_end,
        "handle_delta":  handle_end - handle_start,
        "handle_rate":   post_rate,
        "wset_min":      min(wset), "wset_max": max(wset), "wset_avg": round(statistics.mean(wset), 1),
        "priv_min":      min(priv), "priv_max": max(priv), "priv_avg": round(statistics.mean(priv), 1),
        "avail_min":     min(avail),"avail_max": max(avail),"avail_avg": round(statistics.mean(avail), 1),
        "ffmpeg_min":    min(ffmpeg),"ffmpeg_max": max(ffmpeg),"ffmpeg_avg": round(statistics.mean(ffmpeg), 1),
        "restarts":      restarts,
    }


# ---------------------------------------------------------------------------
# Chart builders — return JS strings for use in <script> block
# Note: data: { } opened with a plain { (not inside f-string) to avoid
# the }} vs } ambiguity in Python f-strings.
# ---------------------------------------------------------------------------

def _handle_chart_js(rows: list[dict]) -> str:
    sampled = _downsample(rows, 1440)
    labels  = [f"{float(r['elapsed_s'])/3600:.3f}" for r in sampled]
    # Replace 0-handle samples with null so Chart.js draws a gap
    data    = [int(r["handle_count"]) if int(r["handle_count"]) > 0 else None for r in sampled]
    lbl_js  = _js_array(labels)
    dat_js  = _js_array(data)
    return "\n".join([
        "new Chart(document.getElementById('handleChart'), {",
        "  type: 'line',",
        f"  data: {{labels: {lbl_js}, datasets: [{{",
        f"    label: 'SRS Handle Count',",
        f"    data: {dat_js},",
        f"    borderColor: '{_DANGER}', backgroundColor: '{_DANGER}18',",
        "     tension: 0.2, borderWidth: 1.5, pointRadius: 0, spanGaps: false",
        "  }]},",
        "  options: { responsive: true, interaction: { mode: 'index', intersect: false },",
        "    scales: {",
        "      x: { title: { display: true, text: 'Elapsed (hours)' }, ticks: { maxTicksLimit: 24, font: { size: 10 } } },",
        "      y: { title: { display: true, text: 'Handle Count' }, ticks: { font: { size: 10 } } }",
        "    },",
        "    plugins: { legend: { position: 'top' } }",
        "  }",
        "});",
    ])


def _memory_chart_js(rows: list[dict]) -> str:
    sampled  = _downsample(rows, 1440)
    labels   = [f"{float(r['elapsed_s'])/3600:.3f}" for r in sampled]
    wset     = [float(r["working_set_mb"]) for r in sampled]
    priv     = [float(r["private_mb"]) for r in sampled]
    avail_gb = [round(float(r["sys_avail_mb"]) / 1024, 3) for r in sampled]
    lbl_js   = _js_array(labels)
    wset_js  = _js_array(wset)
    priv_js  = _js_array(priv)
    avail_js = _js_array(avail_gb)
    return "\n".join([
        "new Chart(document.getElementById('memChart'), {",
        "  type: 'line',",
        f"  data: {{labels: {lbl_js}, datasets: [",
        f"    {{label: 'SRS Working Set (MB)', data: {wset_js}, borderColor: '{_PRIMARY}', backgroundColor: '{_PRIMARY}18', tension: 0.2, borderWidth: 1.5, pointRadius: 0, yAxisID: 'yMB'}},",
        f"    {{label: 'SRS Private Bytes (MB)', data: {priv_js}, borderColor: '{_PURPLE}', backgroundColor: '{_PURPLE}18', tension: 0.2, borderWidth: 1.5, pointRadius: 0, yAxisID: 'yMB'}},",
        f"    {{label: 'Sys Avail RAM (GB)', data: {avail_js}, borderColor: '{_SUCCESS}', backgroundColor: '{_SUCCESS}18', tension: 0.2, borderWidth: 2, pointRadius: 0, yAxisID: 'yGB'}}",
        "  ]},",
        "  options: { responsive: true, interaction: { mode: 'index', intersect: false },",
        "    scales: {",
        "      x: { title: { display: true, text: 'Elapsed (hours)' }, ticks: { maxTicksLimit: 24, font: { size: 10 } } },",
        "      yMB: { position: 'left',  title: { display: true, text: 'MB (SRS process)' }, ticks: { font: { size: 10 } } },",
        "      yGB: { position: 'right', title: { display: true, text: 'GB (Sys Avail RAM)' }, ticks: { font: { size: 10 } }, grid: { drawOnChartArea: false } }",
        "    },",
        "    plugins: { legend: { position: 'top' } }",
        "  }",
        "});",
    ])


def _ffmpeg_chart_js(rows: list[dict]) -> str:
    sampled = _downsample(rows, 720)
    labels  = [f"{float(r['elapsed_s'])/3600:.3f}" for r in sampled]
    data    = [int(r["ffmpeg_count"]) for r in sampled]
    lbl_js  = _js_array(labels)
    dat_js  = _js_array(data)
    return "\n".join([
        "new Chart(document.getElementById('ffmpegChart'), {",
        "  type: 'bar',",
        f"  data: {{labels: {lbl_js}, datasets: [{{",
        f"    label: 'FFmpeg Processes',",
        f"    data: {dat_js},",
        f"    backgroundColor: '{_PRIMARY}80', borderColor: '{_PRIMARY}', borderWidth: 0,",
        "     barPercentage: 1.0, categoryPercentage: 1.0",
        "  }]},",
        "  options: { responsive: true,",
        "    scales: {",
        "      x: { title: { display: true, text: 'Elapsed (hours)' }, ticks: { maxTicksLimit: 24, font: { size: 10 } } },",
        "      y: { title: { display: true, text: 'Process Count' }, beginAtZero: true, ticks: { font: { size: 10 } } }",
        "    },",
        "    plugins: { legend: { position: 'top' } }",
        "  }",
        "});",
    ])


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_html(run_id: str, stats: dict, rows: list[dict]) -> str:
    css_tags, js_tags = _inline_assets()
    logo      = _logo_img_tag(38)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Verdict banner ──────────────────────────────────────────────────────
    delta = stats["handle_delta"]
    rate  = stats["handle_rate"]
    if delta > 50_000 or rate > 5_000:
        verdict_label, verdict_bg, verdict_icon = "LEAK CONFIRMED", _DANGER, "✗"
    elif delta > 10_000:
        verdict_label, verdict_bg, verdict_icon = "GROWTH DETECTED", _WARNING, "⚠"
    else:
        verdict_label, verdict_bg, verdict_icon = "STABLE", _SUCCESS, "✓"

    verdict_html = f"""
    <div style="background:{verdict_bg};color:#fff;border-radius:10px;padding:18px 24px;margin-bottom:1.5rem;">
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
        <div style="font-size:2.2rem;font-weight:800;line-height:1;">{verdict_label}</div>
        <div style="flex:1;min-width:200px;">
          <div style="font-size:.95rem;font-weight:600;">
            {_fmt_duration(stats['duration_s'])} observed &nbsp;|&nbsp;
            post-restart rate: <strong>{rate:,.0f} handles/hr</strong>
          </div>
          <div style="font-size:.82rem;opacity:.9;margin-top:3px;">
            Handles grew <strong>{delta:+,}</strong> from baseline {stats['handle_start']:,} → {stats['handle_end']:,}.
            SRS restarts detected: <strong>{len(stats['restarts'])}</strong>.
          </div>
        </div>
      </div>
    </div>"""

    # ── Stat cards — use same _stat_card() as existing reports ─────────────
    delta_color = _DANGER if delta > 0 else _SUCCESS
    rate_color  = _DANGER if abs(rate) > 5_000 else _WARNING if abs(rate) > 1_000 else _SUCCESS
    avail_color = _DANGER if stats["avail_min"] < 100 else _SUCCESS

    cards_html = "".join([
        _stat_card("Duration",           _fmt_duration(stats["duration_s"])),
        _stat_card("Baseline (post-rst)", f"{stats['handle_start']:,}", accent=_PRIMARY),
        _stat_card("Final Handle Count",  f"{stats['handle_end']:,}",   accent=_DARK),
        _stat_card("Net Growth",          f"{delta:+,}",                accent=delta_color),
        _stat_card("Growth Rate",         f"{rate:,.0f}",   "/hr",      accent=rate_color),
        _stat_card("Peak Handles",        f"{stats['handle_max']:,}",   accent=_DANGER),
        _stat_card("SRS Restarts",        len(stats["restarts"]),        accent=_WARNING if stats["restarts"] else _SUCCESS),
        _stat_card("Sys RAM Min",         f"{stats['avail_min']/1024:.2f}",  " GB", accent=avail_color),
    ])

    # ── Restart events table ────────────────────────────────────────────────
    restart_rows_html = "".join(
        f"<tr>"
        f"<td>{r['timestamp']}</td>"
        f"<td>{_fmt_duration(r['elapsed_s'])}</td>"
        f"<td class='text-end'>{r['from']:,}</td>"
        f"<td class='text-end'>{r['to']:,}</td>"
        f"<td class='text-end' style='color:{_DANGER};font-weight:600;'>{r['drop']:+,}</td>"
        f"</tr>"
        for r in stats["restarts"]
    )
    restart_section = f"""
    <div class="detail-card mb-4">
      <div class="detail-header">SRS Restart Events</div>
      <div class="detail-body p-0">
        <table class="data-table">
          <thead><tr>
            <th>Timestamp</th><th>Elapsed</th>
            <th class="text-end">From</th><th class="text-end">To</th><th class="text-end">Drop</th>
          </tr></thead>
          <tbody>{restart_rows_html}</tbody>
        </table>
      </div>
    </div>""" if stats["restarts"] else ""

    # ── Memory stats table ──────────────────────────────────────────────────
    avail_min_style = f'style="color:{_DANGER};font-weight:600;"' if stats["avail_min"] < 100 else ""
    mem_table_html = f"""
    <div class="detail-card mb-4">
      <div class="detail-header">Memory Statistics</div>
      <div class="detail-body p-0">
        <table class="data-table">
          <thead><tr><th>Metric</th><th class="text-end">Min</th><th class="text-end">Max</th><th class="text-end">Avg</th></tr></thead>
          <tbody>
            <tr><td>SRS Working Set (MB)</td>
                <td class="text-end">{stats['wset_min']:.1f}</td><td class="text-end">{stats['wset_max']:.1f}</td><td class="text-end">{stats['wset_avg']:.1f}</td></tr>
            <tr><td>SRS Private Bytes (MB)</td>
                <td class="text-end">{stats['priv_min']:.1f}</td><td class="text-end">{stats['priv_max']:.1f}</td><td class="text-end">{stats['priv_avg']:.1f}</td></tr>
            <tr><td {avail_min_style}>System Available RAM (GB)</td>
                <td class="text-end" {avail_min_style}>{stats['avail_min']/1024:.2f}</td>
                <td class="text-end">{stats['avail_max']/1024:.2f}</td>
                <td class="text-end">{stats['avail_avg']/1024:.2f}</td></tr>
            <tr><td>FFmpeg Process Count</td>
                <td class="text-end">{stats['ffmpeg_min']}</td><td class="text-end">{stats['ffmpeg_max']}</td><td class="text-end">{stats['ffmpeg_avg']:.1f}</td></tr>
          </tbody>
        </table>
      </div>
    </div>"""

    js_handle  = _handle_chart_js(rows)
    js_memory  = _memory_chart_js(rows)
    js_ffmpeg  = _ffmpeg_chart_js(rows)

    scenario_badge = (
        f"<span style='background:{_DANGER};color:#fff;font-size:.72rem;font-weight:700;"
        f"padding:3px 10px;border-radius:12px;vertical-align:middle;'>SRS Handle Leak</span>"
    )

    # Google Fonts link (matches existing reports)
    fonts_link = "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap' rel='stylesheet'/>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>SRS Handle Leak Report — {run_id}</title>
  {fonts_link}
  {css_tags}
  <style>{_CSS}</style>
</head>
<body>

<div class="report-header">
  <div class="report-header-top">
    <div class="report-title-block">
      {logo}
      <div>
        <h1 class="report-title">ORC Performance Report &nbsp;{scenario_badge}</h1>
        <div class="report-meta">
          {run_id} &nbsp;|&nbsp;
          {stats['first_ts']} → {stats['last_ts']} &nbsp;|&nbsp;
          Generated: {generated}
        </div>
      </div>
    </div>
  </div>
</div>

<div class="container-fluid px-4 py-3">

  {verdict_html}

  <div class="row g-3 mb-4">
    {cards_html}
  </div>

  {restart_section}

  <!-- Handle Count Chart -->
  <div class="detail-card mb-4">
    <div class="detail-header">SRS Handle Count over Time</div>
    <div class="detail-body">
      <canvas id="handleChart" style="max-height:320px;"></canvas>
    </div>
  </div>

  <!-- Memory Chart -->
  <div class="detail-card mb-4">
    <div class="detail-header">Memory over Time</div>
    <div class="detail-body">
      <canvas id="memChart" style="max-height:320px;"></canvas>
    </div>
  </div>

  <!-- FFmpeg Chart -->
  <div class="detail-card mb-4">
    <div class="detail-header">FFmpeg Process Count over Time</div>
    <div class="detail-body">
      <canvas id="ffmpegChart" style="max-height:320px;"></canvas>
    </div>
  </div>

  {mem_table_html}

  <p class="text-muted text-end" style="font-size:.75rem;padding-bottom:2rem;">
    {stats['total_rows']:,} total samples &nbsp;|&nbsp;
    {stats['zero_samples']} zero-handle samples excluded from handle stats &nbsp;|&nbsp;
    Generated {generated}
  </p>

</div>

{js_tags}
<script>
document.addEventListener('DOMContentLoaded', function() {{
{js_handle}
{js_memory}
{js_ffmpeg}
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) >= 2:
        results_dir = sys.argv[1]
    else:
        results_dir = os.path.join(
            os.path.dirname(__file__),
            "results",
            "run_2026-06-04_srs_handle_leak_168h_qa172_01d94e",
        )
        print(f"No results_dir specified — using default:\n  {results_dir}")

    csv_path = os.path.join(results_dir, "srs_handles.csv")
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found")
        sys.exit(1)

    run_id = os.path.basename(results_dir)
    print(f"Loading {csv_path} ...")
    rows  = load_csv(csv_path)
    stats = compute_stats(rows)

    print(f"  {stats['total_rows']} rows  |  duration {_fmt_duration(stats['duration_s'])}")
    print(f"  Handles: {stats['handle_start']:,} → {stats['handle_end']:,}  "
          f"({stats['handle_delta']:+,}  ~{stats['handle_rate']:,.0f}/hr)")
    print(f"  SRS restarts: {len(stats['restarts'])}")

    html = build_html(run_id, stats, rows)

    out_path = os.path.join(results_dir, "report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nReport written → {out_path}")


if __name__ == "__main__":
    main()
