"""
metrics/srs_handle_monitor.py
Monitor SRS process handle count over time via WinRM NTLM.

Purpose
-------
Detect OS handle leaks in the SRS process when ffmpeg child processes
repeatedly fail to connect (bad/unreachable URLs) and are retried by SRS.
Each ffmpeg spawn/death cycle touches OS handle tables; if SRS does not
properly close inherited handles the count will trend upward over time.

Output files (written to output_dir)
-------------------------------------
  srs_handles.csv     -- timestamp, elapsed_s, handle_count, srs_pid, srs_cpu, ffmpeg_count
  srs_handles.log     -- WinRM errors (non-fatal, written at shutdown)
  handle_report.html  -- self-contained Chart.js report
"""

import csv
import os
import threading
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# WinRM PowerShell — SRS handle + ffmpeg count snapshot
# ---------------------------------------------------------------------------

_SRS_HANDLES_SCRIPT = r"""
$srs = Get-Process -Name srs -ErrorAction SilentlyContinue | Select-Object -First 1
$ff  = (Get-Process -Name ffmpeg -ErrorAction SilentlyContinue | Measure-Object).Count
$os  = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$sysTotalMB = if ($os) { [math]::Round($os.TotalVisibleMemorySize / 1KB, 0) } else { 0 }
$sysAvailMB = if ($os) { [math]::Round($os.FreePhysicalMemory      / 1KB, 0) } else { 0 }
if ($srs) {
    $wsMB  = [math]::Round($srs.WorkingSet64        / 1MB, 1)
    $pvMB  = [math]::Round($srs.PrivateMemorySize64 / 1MB, 1)
    Write-Output "$($srs.HandleCount),$($srs.Id),$([math]::Round($srs.CPU, 2)),$ff,$wsMB,$pvMB,$sysTotalMB,$sysAvailMB"
} else {
    Write-Output "0,0,0,$ff,0,0,$sysTotalMB,$sysAvailMB"
}
"""

# ---------------------------------------------------------------------------
# Alert thresholds
# ---------------------------------------------------------------------------

# Rolling window over which handle growth is measured.
_ALERT_WINDOW_S: int   = 600   # 10 minutes
# If handles grow by this many within one window, flag a potential leak.
_ALERT_DELTA:    int   = 50


# ---------------------------------------------------------------------------
# Background collection loop
# ---------------------------------------------------------------------------

def _monitor_loop(
    host: str,
    user: str,
    password: str,
    csv_path: str,
    log_path: str,
    stop_event: threading.Event,
    interval: int = 10,
    alerts_ref: dict | None = None,
) -> None:
    try:
        import winrm
    except ImportError:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("pywinrm not installed — run: pip install pywinrm\n")
        return

    def _make_session():
        return winrm.Session(
            host,
            auth=(user, password),
            transport="ntlm",
            server_cert_validation="ignore",
            read_timeout_sec=30,
            operation_timeout_sec=25,
        )

    try:
        session = _make_session()
    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Failed to create WinRM session to {host}: {exc}\n")
        return

    errors: list[str] = []
    consecutive_errors = 0
    start_ts: datetime | None = None

    # Rolling window: list of (elapsed_s, handle_count) pairs
    window: list[tuple[float, int]] = []

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "elapsed_s", "handle_count", "srs_pid", "srs_cpu", "ffmpeg_count", "working_set_mb", "private_mb", "sys_total_mb", "sys_avail_mb"])

        try:
            while not stop_event.is_set():
                now = datetime.now()
                ts  = now.strftime("%Y-%m-%dT%H:%M:%S")
                if start_ts is None:
                    start_ts = now
                elapsed = (now - start_ts).total_seconds()

                try:
                    r = session.run_ps(_SRS_HANDLES_SCRIPT)
                    if r.status_code == 0:
                        line  = r.std_out.decode("utf-8", errors="replace").strip()
                        parts = line.split(",")
                        if len(parts) >= 4:
                            handle_count  = int(parts[0])
                            srs_pid       = int(parts[1])
                            srs_cpu       = float(parts[2])
                            ffmpeg_count  = int(parts[3])
                            working_set_mb = float(parts[4]) if len(parts) > 4 else 0.0
                            private_mb     = float(parts[5]) if len(parts) > 5 else 0.0
                            sys_total_mb   = float(parts[6]) if len(parts) > 6 else 0.0
                            sys_avail_mb   = float(parts[7]) if len(parts) > 7 else 0.0

                            writer.writerow([ts, round(elapsed, 1), handle_count, srs_pid, srs_cpu, ffmpeg_count, working_set_mb, private_mb, sys_total_mb, sys_avail_mb])
                            f.flush()
                            consecutive_errors = 0

                            # ── Rolling-window leak detection ──────────────
                            window.append((elapsed, handle_count))
                            cutoff = elapsed - _ALERT_WINDOW_S
                            window = [(e, h) for e, h in window if e >= cutoff]

                            if len(window) >= 2:
                                window_delta = window[-1][1] - window[0][1]
                                if window_delta >= _ALERT_DELTA and alerts_ref is not None:
                                    msg = (
                                        f"SRS handles grew by {window_delta} "
                                        f"in {_ALERT_WINDOW_S // 60} min window "
                                        f"({window[0][1]} → {window[-1][1]})"
                                    )
                                    alert = {
                                        "elapsed_s": round(elapsed, 1),
                                        "type":      "HANDLE_LEAK",
                                        "message":   msg,
                                    }
                                    with alerts_ref["lock"]:
                                        existing = alerts_ref["alerts"]
                                        # Avoid alert storm: only emit once per window
                                        if not existing or existing[-1]["elapsed_s"] < elapsed - _ALERT_WINDOW_S:
                                            existing.append(alert)
                                            print(f"  [handle-monitor] ALERT: {msg}")
                                    window.clear()  # reset to avoid cascade
                    else:
                        err = r.std_err.decode("utf-8", errors="replace").strip()[:200]
                        errors.append(f"[{ts}] WinRM error: {err}")
                        consecutive_errors += 1

                except Exception as exc:
                    errors.append(f"[{ts}] {type(exc).__name__}: {str(exc)[:200]}")
                    consecutive_errors += 1

                # Reconnect after 3+ consecutive failures
                if consecutive_errors >= 3:
                    backoff = min(30 * consecutive_errors, 300)
                    errors.append(f"[{ts}] {consecutive_errors} errors — reconnecting in {backoff}s")
                    stop_event.wait(backoff)
                    if not stop_event.is_set():
                        try:
                            session = _make_session()
                        except Exception as exc:
                            errors.append(f"[{ts}] Reconnect failed: {exc}")
                    continue

                stop_event.wait(interval)

        finally:
            pass  # csv file closed by context manager above

    if errors:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("\n".join(errors[:50]))


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class SrsHandleMonitor:
    """Background thread that polls SRS HandleCount via WinRM NTLM."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        csv_path: str,
        log_path: str,
        interval: int = 10,
    ) -> None:
        self._stop   = threading.Event()
        self._alerts = {"lock": threading.Lock(), "alerts": []}
        self._thread = threading.Thread(
            target=_monitor_loop,
            args=(host, user, password, csv_path, log_path, self._stop, interval, self._alerts),
            daemon=True,
            name="srs-handle-monitor",
        )

    def start(self) -> "SrsHandleMonitor":
        self._thread.start()
        return self

    def stop(self, timeout: int = 15) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def get_alerts(self) -> list[dict]:
        with self._alerts["lock"]:
            return list(self._alerts["alerts"])


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def write_handle_report(
    csv_path: str,
    output_dir: str,
    server_name: str = "",
    alerts: list[dict] | None = None,
) -> str:
    """
    Read srs_handles.csv and write a self-contained handle_report.html.

    Returns the absolute path to the generated HTML file, or "" if the CSV
    was empty / missing.
    """
    rows: list[dict] = []
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        "elapsed_h":      round(float(row["elapsed_s"]) / 3600, 5),
                        "handle_count":   int(row["handle_count"]),
                        "srs_cpu":        float(row["srs_cpu"]),
                        "ffmpeg_count":   int(row["ffmpeg_count"]),
                        "working_set_mb": float(row.get("working_set_mb", 0) or 0),
                        "private_mb":     float(row.get("private_mb", 0) or 0),
                        "sys_total_mb":   float(row.get("sys_total_mb", 0) or 0),
                        "sys_avail_mb":   float(row.get("sys_avail_mb", 0) or 0),
                    })
                except (ValueError, KeyError):
                    pass

    if not rows:
        return ""

    elapsed_h      = [r["elapsed_h"]      for r in rows]
    handle_counts  = [r["handle_count"]   for r in rows]
    ffmpeg_counts  = [r["ffmpeg_count"]   for r in rows]
    srs_cpus       = [r["srs_cpu"]        for r in rows]
    working_set_mb = [r["working_set_mb"] for r in rows]
    private_mb     = [r["private_mb"]     for r in rows]
    sys_total_mb   = [r["sys_total_mb"]   for r in rows]
    sys_avail_mb   = [r["sys_avail_mb"]   for r in rows]

    start_handles  = handle_counts[0]
    end_handles    = handle_counts[-1]
    max_handles    = max(handle_counts)
    min_handles    = min(handle_counts)
    delta_handles  = end_handles - start_handles
    duration_h     = elapsed_h[-1] if elapsed_h else 0
    start_ws_mb    = working_set_mb[0]  if working_set_mb else 0
    end_ws_mb      = working_set_mb[-1] if working_set_mb else 0
    delta_ws_mb    = round(end_ws_mb - start_ws_mb, 1)
    delta_ws_class = "text-danger" if delta_ws_mb >= 50 else "text-success"
    sys_total       = sys_total_mb[-1] if sys_total_mb else 0
    sys_avail_start = sys_avail_mb[0]  if sys_avail_mb else 0
    sys_avail_end   = sys_avail_mb[-1] if sys_avail_mb else 0
    sys_used_start  = round(sys_total - sys_avail_start, 0) if sys_total else 0
    sys_used_end    = round(sys_total - sys_avail_end, 0)   if sys_total else 0
    sys_used_pct    = round((sys_used_end / sys_total) * 100, 1) if sys_total else 0
    sys_used_class  = "text-danger" if sys_used_pct >= 90 else ("text-warning" if sys_used_pct >= 75 else "text-success")

    verdict       = "LEAK DETECTED" if delta_handles >= _ALERT_DELTA else "STABLE"
    verdict_class = "danger" if verdict == "LEAK DETECTED" else "success"
    delta_class   = "text-danger" if delta_handles >= _ALERT_DELTA else "text-success"

    alerts = alerts or []

    # ── JS array literals ────────────────────────────────────────────────────
    def _js(lst: list) -> str:
        return "[" + ",".join(str(v) for v in lst) + "]"

    # ── Alert annotation lines for Chart.js annotation plugin ────────────────
    alert_annotations_js = ""
    for a in alerts:
        ah = round(a["elapsed_s"] / 3600, 5)
        alert_annotations_js += f"""
            {{
                type: 'line',
                xMin: {ah}, xMax: {ah},
                borderColor: 'rgba(220,53,69,0.75)',
                borderWidth: 2,
                label: {{
                    display: true,
                    content: 'ALERT',
                    position: 'start',
                    color: '#dc3545',
                    font: {{ size: 10 }}
                }}
            }},"""

    # ── Alert list HTML ───────────────────────────────────────────────────────
    if alerts:
        items_html = "".join(
            f'<li class="list-group-item list-group-item-warning py-2">'
            f'<strong>t={a["elapsed_s"]:.0f}s</strong> &mdash; {a["message"]}</li>'
            for a in alerts
        )
        alerts_section = f"""
  <div class="card p-3 mb-4">
    <h6 class="fw-semibold mb-2 text-danger">&#9888; Leak Alerts ({len(alerts)})</h6>
    <ul class="list-group list-group-flush">{items_html}</ul>
  </div>"""
    else:
        alerts_section = ""

    # ── Point radius: hide dots for dense series ──────────────────────────────
    pt_radius = 0 if len(rows) > 150 else 2

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SRS Handle Leak Test &mdash; {server_name}</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
        crossorigin="anonymous">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
  <style>
    body        {{ background:#f8f9fa; font-family:'Segoe UI',system-ui,sans-serif; }}
    .card       {{ border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
    .stat-val   {{ font-size:2rem; font-weight:700; }}
    .stat-lbl   {{ font-size:.8rem; color:#6c757d; text-transform:uppercase; letter-spacing:.05em; }}
    .chart-wrap {{ position:relative; height:320px; }}
  </style>
</head>
<body>
<div class="container-fluid py-4" style="max-width:1400px">

  <!-- Header -->
  <div class="d-flex flex-wrap align-items-center gap-3 mb-4">
    <h4 class="mb-0 fw-bold">SRS Handle Leak Test</h4>
    <span class="badge bg-{verdict_class} fs-6 px-3 py-2">{verdict}</span>
    <span class="ms-auto text-muted small">
      Server: <strong>{server_name}</strong>
      &nbsp;|&nbsp; Duration: <strong>{duration_h:.2f} h</strong>
      &nbsp;|&nbsp; Samples: <strong>{len(rows):,}</strong>
    </span>
  </div>

  <!-- Stat cards -->
  <div class="row g-3 mb-4">
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-primary">{start_handles:,}</div>
        <div class="stat-lbl">Start Handles</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-primary">{end_handles:,}</div>
        <div class="stat-lbl">End Handles</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val {delta_class}">{delta_handles:+,}</div>
        <div class="stat-lbl">Net Change</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-warning">{max_handles:,}</div>
        <div class="stat-lbl">Peak Handles</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-info">{start_ws_mb:.0f} MB</div>
        <div class="stat-lbl">Start Working Set</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-info">{end_ws_mb:.0f} MB</div>
        <div class="stat-lbl">End Working Set</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val {delta_ws_class}">{delta_ws_mb:+.1f} MB</div>
        <div class="stat-lbl">Memory Net Change</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-secondary">{sys_total:.0f} MB</div>
        <div class="stat-lbl">System Total RAM</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-secondary">{sys_used_start:.0f} MB</div>
        <div class="stat-lbl">Sys Used RAM (start)</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val text-secondary">{sys_used_end:.0f} MB</div>
        <div class="stat-lbl">Sys Used RAM (end)</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3 text-center">
        <div class="stat-val {sys_used_class}">{sys_used_pct:.1f}%</div>
        <div class="stat-lbl">Sys RAM Used (final)</div>
      </div>
    </div>
  </div>

  <!-- Handle count chart -->
  <div class="card p-3 mb-4">
    <h6 class="fw-semibold mb-3">SRS Handle Count Over Time</h6>
    <div class="chart-wrap"><canvas id="handleChart"></canvas></div>
  </div>

  <!-- SRS Memory chart -->
  <div class="card p-3 mb-4">
    <h6 class="fw-semibold mb-3">SRS Memory Over Time</h6>
    <div class="chart-wrap"><canvas id="memChart"></canvas></div>
  </div>

  <!-- System Memory chart -->
  <div class="card p-3 mb-4">
    <h6 class="fw-semibold mb-3">System Memory Over Time</h6>
    <div class="chart-wrap"><canvas id="sysMemChart"></canvas></div>
  </div>

  <!-- ffmpeg count + SRS CPU chart -->
  <div class="card p-3 mb-4">
    <h6 class="fw-semibold mb-3">ffmpeg Process Count &amp; SRS CPU Over Time</h6>
    <div class="chart-wrap"><canvas id="ffmpegChart"></canvas></div>
  </div>

  <!-- Alerts section -->
  {alerts_section}

  <div class="text-muted small text-end mt-2">
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    &nbsp;|&nbsp; Alert threshold: +{_ALERT_DELTA} handles in {_ALERT_WINDOW_S // 60} min
  </div>

</div><!-- /container -->

<script>
Chart.register(ChartAnnotation);

const elapsed    = {_js(elapsed_h)};
const handles    = {_js(handle_counts)};
const ffmpegs    = {_js(ffmpeg_counts)};
const srsCpu     = {_js(srs_cpus)};
const workingSet = {_js(working_set_mb)};
const privateMem = {_js(private_mb)};
const sysTotalMB = {_js(sys_total_mb)};
const sysAvailMB = {_js(sys_avail_mb)};
const sysUsedMB  = sysTotalMB.map((t, i) => t > 0 ? parseFloat((t - sysAvailMB[i]).toFixed(0)) : 0);

// ── Handle count chart ──────────────────────────────────────────────────────
new Chart(document.getElementById('handleChart'), {{
  type: 'line',
  data: {{
    labels: elapsed,
    datasets: [{{
      label: 'SRS Handle Count',
      data: handles,
      borderColor:     'rgba(13,110,253,0.9)',
      backgroundColor: 'rgba(13,110,253,0.08)',
      fill: true,
      tension: 0.2,
      pointRadius: {pt_radius},
      borderWidth: 2,
    }}]
  }},
  options: {{
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'top' }},
      annotation: {{
        annotations: [{alert_annotations_js}]
      }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Elapsed (hours)' }} }},
      y: {{
        title:      {{ display: true, text: 'Handle Count' }},
        beginAtZero: false,
        ticks:      {{ maxTicksLimit: 8 }},
      }}
    }}
  }}
}});

// ── Memory chart ──────────────────────────────────────────────────────────
new Chart(document.getElementById('memChart'), {{
  type: 'line',
  data: {{
    labels: elapsed,
    datasets: [
      {{
        label:           'Working Set (MB)',
        data:            workingSet,
        borderColor:     'rgba(13,202,240,0.9)',
        backgroundColor: 'rgba(13,202,240,0.08)',
        fill: true,
        tension: 0.2,
        pointRadius: {pt_radius},
        borderWidth: 2,
        yAxisID: 'yMem',
      }},
      {{
        label:           'Private Memory (MB)',
        data:            privateMem,
        borderColor:     'rgba(111,66,193,0.9)',
        backgroundColor: 'transparent',
        tension: 0.2,
        pointRadius: {pt_radius},
        borderWidth: 2,
        yAxisID: 'yMem',
      }}
    ]
  }},
  options: {{
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{
      x:    {{ title: {{ display: true, text: 'Elapsed (hours)' }} }},
      yMem: {{
        type: 'linear', position: 'left',
        title: {{ display: true, text: 'Memory (MB)' }},
        beginAtZero: false,
        ticks: {{ maxTicksLimit: 8 }},
      }}
    }}
  }}
}});

// ── System memory chart ────────────────────────────────────────────────────
new Chart(document.getElementById('sysMemChart'), {{
  type: 'line',
  data: {{
    labels: elapsed,
    datasets: [
      {{
        label:           'System Used (MB)',
        data:            sysUsedMB,
        borderColor:     'rgba(220,53,69,0.9)',
        backgroundColor: 'rgba(220,53,69,0.08)',
        fill: true,
        tension: 0.2,
        pointRadius: {pt_radius},
        borderWidth: 2,
        yAxisID: 'ySysMem',
      }},
      {{
        label:           'System Available (MB)',
        data:            sysAvailMB,
        borderColor:     'rgba(25,135,84,0.9)',
        backgroundColor: 'transparent',
        tension: 0.2,
        pointRadius: {pt_radius},
        borderWidth: 2,
        yAxisID: 'ySysMem',
      }}
    ]
  }},
  options: {{
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{
      x:       {{ title: {{ display: true, text: 'Elapsed (hours)' }} }},
      ySysMem: {{
        type: 'linear', position: 'left',
        title: {{ display: true, text: 'Memory (MB)' }},
        beginAtZero: false,
        ticks: {{ maxTicksLimit: 8 }},
      }}
    }}
  }}
}});

// ── ffmpeg count + SRS CPU chart ────────────────────────────────────────────
new Chart(document.getElementById('ffmpegChart'), {{
  type: 'line',
  data: {{
    labels: elapsed,
    datasets: [
      {{
        label:           'ffmpeg Process Count',
        data:            ffmpegs,
        borderColor:     'rgba(255,153,0,0.9)',
        backgroundColor: 'rgba(255,153,0,0.08)',
        fill: true,
        tension: 0.2,
        pointRadius: {pt_radius},
        yAxisID: 'yLeft',
      }},
      {{
        label:           'SRS CPU (cumulative seconds)',
        data:            srsCpu,
        borderColor:     'rgba(25,135,84,0.9)',
        backgroundColor: 'transparent',
        tension: 0.2,
        pointRadius: {pt_radius},
        yAxisID: 'yRight',
      }}
    ]
  }},
  options: {{
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{
      x:      {{ title: {{ display: true, text: 'Elapsed (hours)' }} }},
      yLeft:  {{
        type: 'linear', position: 'left',
        title: {{ display: true, text: 'ffmpeg Count' }},
        beginAtZero: true,
      }},
      yRight: {{
        type: 'linear', position: 'right',
        title: {{ display: true, text: 'SRS CPU (s)' }},
        beginAtZero: true,
        grid: {{ drawOnChartArea: false }}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

    out_path = os.path.join(output_dir, "handle_report.html")
    with open(out_path, "w", encoding="utf-8") as fout:
        fout.write(html)
    return out_path
