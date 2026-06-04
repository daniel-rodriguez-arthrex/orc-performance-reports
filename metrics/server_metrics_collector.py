"""
metrics/server_metrics_collector.py
Python-native server metrics collector using pywinrm NTLM transport.
Replaces the PowerShell subprocess approach — no TrustedHosts or domain
membership required on the client machine.
"""
import csv
import re
import threading
import time
from datetime import datetime

_METRICS_SCRIPT = r"""
$cpu = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples.CookedValue, 2)
$os = Get-CimInstance Win32_OperatingSystem
$ramTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$ramUsed  = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 2)
$net = Get-WmiObject Win32_PerfFormattedData_Tcpip_NetworkInterface
$recv = [math]::Round(($net | Measure-Object BytesReceivedPersec -Sum).Sum / 125000, 3)
$sent = [math]::Round(($net | Measure-Object BytesSentPersec -Sum).Sum / 125000, 3)
Write-Output "$cpu,$ramUsed,$ramTotal,$recv,$sent"
"""

_STRIP_XML = re.compile(r"<[^>]+>")

# Collected every poll cycle (not just on spikes) to build per-process time-series charts.
# Returns a single comma-delimited line:
#   srs_cpu,srs_ram_mb,srs_virt_mb,ffmpeg_count,ffmpeg_cpu,ffmpeg_ram_mb,ffmpeg_virt_mb
_PROC_SERIES_SCRIPT = r"""
$cores = (Get-WmiObject Win32_ComputerSystem).NumberOfLogicalProcessors
if (-not $cores -or $cores -eq 0) { $cores = 1 }
$procs = Get-WmiObject Win32_PerfFormattedData_PerfProc_Process |
    Where-Object { $_.Name -ne '_Total' -and $_.Name -ne 'Idle' }

# SRS process (single instance)
$srs = $procs | Where-Object { $_.Name -eq 'srs' } | Select-Object -First 1
$srsCpu  = if ($srs) { [math]::Round($srs.PercentProcessorTime / $cores, 2) } else { 0 }
$srsRam  = if ($srs) { [math]::Round($srs.WorkingSet  / 1MB, 1) } else { 0 }
$srsVirt = if ($srs) { [math]::Round($srs.VirtualBytes / 1MB, 1) } else { 0 }

# All ffmpeg instances (stream ingesters) — summed, CPU normalized to 0-100% scale
$ff       = $procs | Where-Object { $_.Name -match '^ffmpeg' }
$ffCount  = ($ff | Measure-Object).Count
$ffCpu    = [math]::Round(($ff | Measure-Object PercentProcessorTime -Sum).Sum / $cores, 2)
$ffRam    = [math]::Round(($ff | Measure-Object WorkingSet  -Sum).Sum / 1MB, 1)
$ffVirt   = [math]::Round(($ff | Measure-Object VirtualBytes -Sum).Sum / 1MB, 1)

Write-Output "$srsCpu,$srsRam,$srsVirt,$ffCount,$ffCpu,$ffRam,$ffVirt"
"""

# Per-ffmpeg instance details — PID, CPU, RAM, virtual memory, and stream URL extracted
# from CommandLine. Run every N polls (not every poll) to keep WinRM overhead low.
# Output: one pipe-delimited line per running ffmpeg instance:
#   pid|cpu_pct|ram_mb|virt_mb|url
_FFMPEG_INSTANCES_SCRIPT = r"""
$cores = (Get-WmiObject Win32_ComputerSystem).NumberOfLogicalProcessors
if (-not $cores -or $cores -eq 0) { $cores = 1 }
# Match Win32_Process (CommandLine) with Win32_PerfFormattedData (CPU/RAM) by index.
# WMI ProcessId is unreliable over WinRM NTLM — use index-based matching instead.
#
# WMI assigns perf-counter name suffixes in creation order:
#   first instance  -> "ffmpeg"    (no suffix)
#   second instance -> "ffmpeg#1"
#   third instance  -> "ffmpeg#2"  ...
# So sorting $ffPerf by that numeric suffix (-1 for the bare name) matches the
# chronological CreationDate order of $ffProcs exactly.
$ffProcs = Get-WmiObject Win32_Process |
    Where-Object { $_.Name -match '^ffmpeg' } |
    Sort-Object CreationDate
$ffPerf  = Get-WmiObject Win32_PerfFormattedData_PerfProc_Process |
    Where-Object { $_.Name -match '^ffmpeg' } |
    Sort-Object { if ($_.Name -match '#(\d+)$') { [int]$Matches[1] } else { -1 } }
$count = [math]::Min($ffProcs.Count, $ffPerf.Count)
for ($i = 0; $i -lt $count; $i++) {
    $proc = $ffProcs[$i]
    $perf = $ffPerf[$i]
    $idx  = $i + 1
    $cpu  = [math]::Round($perf.PercentProcessorTime / $cores, 2)
    $ram  = [math]::Round($perf.WorkingSet  / 1MB, 1)
    $virt = [math]::Round($perf.VirtualBytes / 1MB, 1)
    $cmd  = $proc.CommandLine
    $url  = ''
    if ($cmd -and $cmd -match '(rtsps?://\S+|rtmps?://\S+|https?://\S+)') {
        # Strip embedded credentials (user:pass@) before storing
        $url = $Matches[1] -replace '^(rtsps?|rtmps?|https?)://[^@]+@', '$1://'
    }
    Write-Output "$idx|$cpu|$ram|$virt|$url"
}
"""

_PROCESS_SCRIPT = r"""
$allProcs = Get-WmiObject Win32_PerfFormattedData_PerfProc_Process |
    Where-Object { $_.Name -ne '_Total' -and $_.Name -ne 'Idle' }
# Top-10 by CPU + top-5 by WorkingSet, deduplicated by PID.
# Capturing both ensures RAM-heavy-but-CPU-idle processes (e.g. large caches, idle workers)
# are visible even when they don't rank in the CPU top-10 during a spike.
$byCpu = $allProcs | Sort-Object PercentProcessorTime -Descending | Select-Object -First 10
$byRam = $allProcs | Sort-Object WorkingSet -Descending | Select-Object -First 5
$cmdLines = @{}
Get-WmiObject Win32_Process | ForEach-Object {
    $cmdLines[[int]$_.ProcessId] = $_.CommandLine
}
$seen = @{}; $procs = @()
foreach ($p in ($byCpu + $byRam)) {
    if (-not $seen.ContainsKey([int]$p.IDProcess)) {
        $seen[[int]$p.IDProcess] = $true; $procs += $p
    }
}
foreach ($p in $procs) {
    $raw = $cmdLines[[int]$p.IDProcess]
    # Strip newlines/pipes that would break our delimiter; truncate to 600 chars
    if ($raw) { $raw = ($raw -replace '[\r\n\|]', ' ').Trim() }
    if ($raw -and $raw.Length -gt 600) { $raw = $raw.Substring(0, 600) }
    $cmd = if ($raw) { $raw } else { '' }
    Write-Output "$($p.Name)|$($p.IDProcess)|$($p.PercentProcessorTime)|$([math]::Round($p.WorkingSet/1MB,1))|$cmd"
}
"""


def _collect_loop(host, user, password, csv_path, log_path, stop_event, interval=5,
                  latest_ref=None, spikes_csv_path=None, spike_threshold=70.0,
                  proc_series_csv_path=None, ffmpeg_instances_csv_path=None,
                  ffmpeg_instances_interval=6):
    try:
        import winrm
    except ImportError:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("pywinrm not installed — run: pip install pywinrm\n")
        return

    try:
        session = winrm.Session(
            host,
            auth=(user, password),
            transport="ntlm",
            server_cert_validation="ignore",
            read_timeout_sec=30,
            operation_timeout_sec=25,
        )
    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Failed to create WinRM session to {host}: {exc}\n")
        return

    errors = []
    consecutive_errors = 0
    consecutive_zero_cpu = 0   # WMI stale-data detector
    _ZERO_CPU_WARN = 5         # warn after this many consecutive 0.0% CPU readings
    MAX_BACKOFF = 300  # cap reconnect wait at 5 minutes

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
    spikes_file = None
    spikes_writer = None
    if spikes_csv_path:
        spikes_file = open(spikes_csv_path, "w", newline="", encoding="utf-8")
        spikes_writer = csv.writer(spikes_file)
        spikes_writer.writerow(["timestamp", "server_cpu_percent", "process_name",
                                 "pid", "proc_cpu_percent", "working_set_mb", "cmd_line"])

    proc_series_file = None
    proc_series_writer = None
    if proc_series_csv_path:
        proc_series_file = open(proc_series_csv_path, "w", newline="", encoding="utf-8")
        proc_series_writer = csv.writer(proc_series_file)
        proc_series_writer.writerow(["timestamp", "srs_cpu", "srs_ram_mb", "srs_virt_mb",
                                      "ffmpeg_count", "ffmpeg_cpu", "ffmpeg_ram_mb", "ffmpeg_virt_mb"])

    ff_instances_file = None
    ff_instances_writer = None
    if ffmpeg_instances_csv_path:
        ff_instances_file = open(ffmpeg_instances_csv_path, "w", newline="", encoding="utf-8")
        ff_instances_writer = csv.writer(ff_instances_file)
        ff_instances_writer.writerow(["timestamp", "stream_idx", "cpu_pct", "ram_mb", "virt_mb", "url"])

    ff_poll_counter = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "cpu_percent", "ram_used_gb", "ram_total_gb",
                          "net_recv_mbps", "net_send_mbps"])

        try:
            while not stop_event.is_set():
                try:
                    r = session.run_ps(_METRICS_SCRIPT)
                    if r.status_code == 0:
                        line = r.std_out.decode("utf-8", errors="replace").strip()
                        parts = line.split(",")
                        if len(parts) == 5:
                            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                            writer.writerow([ts] + parts)
                            f.flush()
                            consecutive_errors = 0  # reset on success
                            cpu_val = None
                            if latest_ref is not None:
                                try:
                                    ram_used  = float(parts[1])
                                    ram_total = float(parts[2])
                                    ram_pct   = round(ram_used / ram_total * 100, 1) if ram_total else 0
                                    cpu_val   = float(parts[0])
                                except (ValueError, ZeroDivisionError):
                                    ram_pct = 0
                                # WMI stale-data detection: warn if CPU reads exactly 0.0
                                # for several consecutive polls (provider may have gone stale)
                                if cpu_val == 0.0:
                                    consecutive_zero_cpu += 1
                                    if consecutive_zero_cpu == _ZERO_CPU_WARN:
                                        errors.append(
                                            f"[wmi-warn] CPU reported 0.0% for "
                                            f"{consecutive_zero_cpu} consecutive polls — "
                                            f"WMI provider may be stale"
                                        )
                                else:
                                    consecutive_zero_cpu = 0
                                import threading as _th, time as _tm
                                with latest_ref["lock"]:
                                    latest_ref["cpu_percent"]  = cpu_val
                                    latest_ref["ram_pct"]      = ram_pct
                                    latest_ref["last_updated"] = _tm.monotonic()
                            # Per-process time-series — collected every poll cycle
                            if proc_series_writer:
                                try:
                                    rps = session.run_ps(_PROC_SERIES_SCRIPT)
                                    if rps.status_code == 0:
                                        ps_line = rps.std_out.decode("utf-8", errors="replace").strip()
                                        ps_parts = ps_line.split(",")
                                        if len(ps_parts) == 7:
                                            proc_series_writer.writerow([ts] + ps_parts)
                                            proc_series_file.flush()
                                except Exception:
                                    pass
                            # Per-ffmpeg instance details — every Nth poll
                            ff_poll_counter += 1
                            if ff_instances_writer and ff_poll_counter % ffmpeg_instances_interval == 0:
                                try:
                                    rfi = session.run_ps(_FFMPEG_INSTANCES_SCRIPT)
                                    if rfi.status_code == 0:
                                        for inst_line in rfi.std_out.decode("utf-8", errors="replace").strip().splitlines():
                                            fi = inst_line.strip().split("|", 4)
                                            if len(fi) >= 4:
                                                url = fi[4] if len(fi) == 5 else ""
                                                ff_instances_writer.writerow([ts] + fi[:4] + [url])
                                        ff_instances_file.flush()
                                except Exception:
                                    pass
                            # Capture top processes on CPU spike
                            if spikes_writer and cpu_val is not None and cpu_val >= spike_threshold:
                                try:
                                    rp = session.run_ps(_PROCESS_SCRIPT)
                                    if rp.status_code == 0:
                                        proc_lines = rp.std_out.decode("utf-8", errors="replace").strip().splitlines()
                                        for pl in proc_lines:
                                            # Split on first 4 | only — cmd_line may contain | itself
                                            fields = pl.strip().split("|", 4)
                                            if len(fields) >= 4:
                                                cmd = fields[4] if len(fields) == 5 else ""
                                                spikes_writer.writerow([ts, parts[0]] + fields[:4] + [cmd])
                                        spikes_file.flush()
                                except Exception:
                                    pass
                    else:
                        err = _STRIP_XML.sub("", r.std_err.decode("utf-8", errors="replace")).strip()
                        errors.append(err[:200])
                        consecutive_errors += 1
                except Exception as exc:
                    errors.append(str(exc)[:200])
                    consecutive_errors += 1

                # Recreate the session after 3+ consecutive errors (broken connection)
                if consecutive_errors >= 3:
                    backoff = min(30 * consecutive_errors, MAX_BACKOFF)
                    errors.append(f"[reconnect] {consecutive_errors} errors — waiting {backoff}s then recreating session")
                    stop_event.wait(backoff)
                    if not stop_event.is_set():
                        try:
                            session = _make_session()
                        except Exception as exc:
                            errors.append(f"[reconnect] session creation failed: {exc}")
                    continue

                stop_event.wait(interval)
        finally:
            if spikes_file:
                spikes_file.close()
            if proc_series_file:
                proc_series_file.close()
            if ff_instances_file:
                ff_instances_file.close()

    if errors:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(errors[:20]))


class ServerMetricsCollector:
    """Background thread that polls server CPU/RAM/network via WinRM NTLM."""

    def __init__(self, host, user, password, csv_path, log_path, interval=5,
                 spikes_csv_path=None, spike_threshold=70.0, proc_series_csv_path=None,
                 ffmpeg_instances_csv_path=None, ffmpeg_instances_interval=6):
        self._stop   = threading.Event()
        self._latest = {"lock": threading.Lock(), "cpu_percent": None, "ram_pct": None,
                        "last_updated": None}
        self._thread = threading.Thread(
            target=_collect_loop,
            args=(host, user, password, csv_path, log_path, self._stop, interval,
                  self._latest, spikes_csv_path, spike_threshold, proc_series_csv_path,
                  ffmpeg_instances_csv_path, ffmpeg_instances_interval),
            daemon=True,
            name="server-metrics",
        )

    def get_latest(self, max_age_s: float = 120.0) -> dict:
        """Return the most recent {cpu_percent, ram_pct} snapshot.
        Returns empty dict if data is older than max_age_s (collector has died)."""
        import time as _time
        with self._latest["lock"]:
            ts = self._latest.get("last_updated")
            if ts is not None and (_time.monotonic() - ts) > max_age_s:
                return {}  # stale — collector is no longer updating
            return {k: v for k, v in self._latest.items() if k not in ("lock", "last_updated")}

    def start(self):
        self._thread.start()
        return self

    def stop(self, timeout=15):
        self._stop.set()
        self._thread.join(timeout=timeout)
