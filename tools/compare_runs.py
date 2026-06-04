"""
compare_runs.py — Side-by-side comparison of two ORC performance test runs.

Usage:
    python compare_runs.py <run_dir_1> <run_dir_2>

Example:
    python compare_runs.py \
        results/run_2026-05-21_endurance_5day/endurance_test_20260521_153954 \
        results/endurance_test_qa162_4d_20260529_134635
"""

import csv
import os
import sys
import statistics
from datetime import datetime


# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _arrow(delta, lower_is_better=False):
    """Return a coloured arrow + delta string."""
    if delta is None:
        return ""
    sign = "▲" if delta > 0 else "▼"
    colour = (RED if delta > 0 else GREEN) if not lower_is_better else (GREEN if delta > 0 else RED)
    if abs(delta) < 0.05:
        colour = YELLOW
        sign = "~"
    return f"  {colour}{sign}{delta:+.1f}{RESET}"


def _pct_arrow(delta, lower_is_better=False):
    """Like _arrow but appends a % sign."""
    if delta is None:
        return ""
    sign = "▲" if delta > 0 else "▼"
    colour = (RED if delta > 0 else GREEN) if not lower_is_better else (GREEN if delta > 0 else RED)
    if abs(delta) < 0.05:
        colour = YELLOW
        sign = "~"
    return f"  {colour}{sign}{abs(delta):.1f}%{RESET}"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_run(run_dir):
    """Return a dict of stats for a single run directory."""
    data = {"run_dir": run_dir}

    # ── summary.csv (pre-computed) ───────────────────────────────────────────
    summary_path = os.path.join(run_dir, "summary.csv")
    if os.path.exists(summary_path):
        with open(summary_path, newline="") as f:
            row = next(csv.DictReader(f), {})
        data["server_name"]      = row.get("server_name", "?")
        data["hardware"]         = row.get("hardware", "?")
        data["session_tier"]     = int(row.get("session_tier") or 0)
        data["avg_cpu_pct"]      = float(row.get("avg_cpu_pct") or 0)
        data["max_cpu_pct"]      = float(row.get("max_cpu_pct") or 0)
        data["avg_ram_gb"]       = float(row.get("avg_ram_gb") or 0)
        data["avg_net_recv_mbps"]= float(row.get("avg_net_recv_mbps") or 0)

    # ── server_metrics.csv (re-computed for p99, peak RAM, duration) ─────────
    sm_path = os.path.join(run_dir, "server_metrics.csv")
    if os.path.exists(sm_path):
        cpu_vals, ram_pct_vals, ram_gb_vals, net_vals = [], [], [], []
        timestamps = []
        with open(sm_path, newline="") as f:
            for row in csv.DictReader(f):
                cpu_vals.append(float(row["cpu_percent"]))
                ram_used = float(row.get("ram_used_gb", 0))
                ram_total = float(row.get("ram_total_gb", 32))
                ram_pct_vals.append(ram_used / ram_total * 100 if ram_total else 0)
                ram_gb_vals.append(ram_used)
                net_vals.append(float(row.get("net_recv_mbps", 0)))
                timestamps.append(datetime.fromisoformat(row["timestamp"]))

        if timestamps:
            duration_s = (timestamps[-1] - timestamps[0]).total_seconds()
            data["duration_h"]    = round(duration_s / 3600, 1)
            data["start_ts"]      = timestamps[0]
            data["end_ts"]        = timestamps[-1]
            data["sample_count"]  = len(cpu_vals)

        if cpu_vals:
            sorted_cpu = sorted(cpu_vals)
            p99_idx = int(len(sorted_cpu) * 0.99)
            data["p99_cpu_pct"]   = round(sorted_cpu[p99_idx], 2)
            data["avg_cpu_pct"]   = round(statistics.mean(cpu_vals), 1)   # override summary
            data["max_cpu_pct"]   = round(max(cpu_vals), 1)
            data["pct_above_80"]  = round(sum(1 for c in cpu_vals if c >= 80) / len(cpu_vals) * 100, 1)
            data["pct_above_90"]  = round(sum(1 for c in cpu_vals if c >= 90) / len(cpu_vals) * 100, 1)
            data["ram_total_gb"]  = ram_total
            data["avg_ram_pct"]   = round(statistics.mean(ram_pct_vals), 1)
            data["peak_ram_gb"]   = round(max(ram_gb_vals), 1)
            data["peak_ram_pct"]  = round(max(ram_pct_vals), 1)
            data["avg_ram_gb"]    = round(statistics.mean(ram_gb_vals), 1)
            data["avg_net_recv"]  = round(statistics.mean(net_vals), 1)

    # ── proc_series.csv (ffmpeg peak count, avg srs cpu) ────────────────────
    ps_path = os.path.join(run_dir, "proc_series.csv")
    if os.path.exists(ps_path):
        ff_counts, srs_cpus = [], []
        with open(ps_path, newline="") as f:
            for row in csv.DictReader(f):
                ff_counts.append(int(float(row.get("ffmpeg_count", 0))))
                srs_cpus.append(float(row.get("srs_cpu", 0)))
        if ff_counts:
            data["peak_ffmpeg"]   = max(ff_counts)
            data["avg_ffmpeg"]    = round(statistics.mean(ff_counts), 1)
            data["avg_srs_cpu"]   = round(statistics.mean(srs_cpus), 1)

    # ── ffmpeg_instances.csv (per-type breakdown, if present) ───────────────
    fi_path = os.path.join(run_dir, "ffmpeg_instances.csv")
    if os.path.exists(fi_path):
        # Group all cpu_pct samples by distinct URL, then classify by type
        url_cpus: dict = {}
        with open(fi_path, newline="") as f:
            for row in csv.DictReader(f):
                url = (row.get("url") or "").lower()
                cpu = float(row.get("cpu_pct") or row.get("avg_cpu_pct") or 0)
                url_cpus.setdefault(url, []).append(cpu)

        type_url_avgs: dict = {}
        for url, cpus in url_cpus.items():
            if url.startswith("rtsps://"):                          ft = "Vision"
            elif url.startswith("rtmp://"):                         ft = "Matrix"
            elif ":8554/orc/" in url:                               ft = "Sim"
            elif "/cameravideo" in url:                             ft = "UHD4"
            elif "/media/video" in url or "/axis-media/" in url:   ft = "Sony"
            else:                                                    ft = "Sim"
            type_url_avgs.setdefault(ft, []).append(sum(cpus) / len(cpus))

        data["ffmpeg_by_type"] = {
            k: {"count": len(v), "avg_cpu_pct": round(sum(v) / len(v), 2)}
            for k, v in type_url_avgs.items()
        }

    return data


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt(val, unit="", precision=1, missing="—"):
    if val is None:
        return missing
    if unit:
        return f"{val:.{precision}f} {unit}"
    return f"{val:.{precision}f}"


def _row(label, v1, v2, unit="", delta=None, lower_is_better=False, precision=1):
    w = 28
    col1 = _fmt(v1, unit, precision)
    col2 = _fmt(v2, unit, precision)
    arrow = ""
    if delta is not None and v1 is not None and v2 is not None:
        d = v2 - v1
        arrow = _pct_arrow(d, lower_is_better) if unit == "%" else _arrow(d, lower_is_better)
    print(f"  {label:<{w}} {col1:<18} {col2:<18}{arrow}")


def _section(title):
    print(f"\n{BOLD}{CYAN}{'─'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*70}{RESET}")


# ── Main ─────────────────────────────────────────────────────────────────────

def compare(dir1, dir2):
    print(f"\n{BOLD}Loading run 1: {dir1}{RESET}")
    a = load_run(dir1)
    print(f"{BOLD}Loading run 2: {dir2}{RESET}")
    b = load_run(dir2)

    label_a = os.path.basename(dir1.rstrip("/\\"))
    label_b = os.path.basename(dir2.rstrip("/\\"))

    # Truncate long labels
    max_lbl = 22
    label_a = label_a[:max_lbl] if len(label_a) > max_lbl else label_a
    label_b = label_b[:max_lbl] if len(label_b) > max_lbl else label_b

    print(f"\n{'─'*70}")
    print(f"  {'METRIC':<28} {BOLD}{label_a:<18}{RESET} {BOLD}{label_b:<18}{RESET}")
    print(f"{'─'*70}")

    _section("Run Identity")
    print(f"  {'Server':<28} {a.get('server_name','?'):<18} {b.get('server_name','?')}")
    print(f"  {'Hardware':<28} {a.get('hardware','?'):<18} {b.get('hardware','?')}")
    _row("Sources (session tier)",   a.get("session_tier"), b.get("session_tier"), precision=0)

    def _fmt_ts(ts):
        return ts.strftime("%Y-%m-%d %H:%M") if ts else "—"

    def _fmt_dur(h):
        if h is None: return "—"
        days = int(h // 24); hrs = h % 24
        return f"{days}d {hrs:.1f}h" if days else f"{hrs:.1f}h"

    ts_a_start = a.get("start_ts"); ts_a_end = a.get("end_ts")
    ts_b_start = b.get("start_ts"); ts_b_end = b.get("end_ts")
    print(f"  {'Start':<28} {_fmt_ts(ts_a_start):<18} {_fmt_ts(ts_b_start)}")
    print(f"  {'End':<28} {_fmt_ts(ts_a_end):<18} {_fmt_ts(ts_b_end)}")
    print(f"  {'Duration':<28} {_fmt_dur(a.get('duration_h')):<18} {_fmt_dur(b.get('duration_h'))}")
    _row("Sample points",            a.get("sample_count"), b.get("sample_count"), precision=0)

    _section("CPU")
    _row("Avg CPU",       a.get("avg_cpu_pct"),  b.get("avg_cpu_pct"),  unit="%", delta=True)
    _row("p99 CPU",       a.get("p99_cpu_pct"),  b.get("p99_cpu_pct"),  unit="%", delta=True)
    _row("Max CPU",       a.get("max_cpu_pct"),  b.get("max_cpu_pct"),  unit="%", delta=True)
    _row("Time ≥ 80%",    a.get("pct_above_80"), b.get("pct_above_80"), unit="%", delta=True)
    _row("Time ≥ 90%",    a.get("pct_above_90"), b.get("pct_above_90"), unit="%", delta=True)

    _section("RAM")
    ram_total_a = a.get("ram_total_gb", 32)
    ram_total_b = b.get("ram_total_gb", 32)
    print(f"  {'Total RAM':<28} {ram_total_a:.0f} GB{'':<14} {ram_total_b:.0f} GB")
    _row("Avg RAM",       a.get("avg_ram_gb"),   b.get("avg_ram_gb"),   unit="GB", delta=True)
    _row("Avg RAM %",     a.get("avg_ram_pct"),  b.get("avg_ram_pct"),  unit="%",  delta=True)
    _row("Peak RAM",      a.get("peak_ram_gb"),  b.get("peak_ram_gb"),  unit="GB", delta=True)
    _row("Peak RAM %",    a.get("peak_ram_pct"), b.get("peak_ram_pct"), unit="%",  delta=True)

    _section("Network")
    _row("Avg Recv",      a.get("avg_net_recv"),  b.get("avg_net_recv"),  unit="Mbps", delta=True, lower_is_better=False)

    _section("Process Health")
    _row("Peak ffmpeg",   a.get("peak_ffmpeg"),   b.get("peak_ffmpeg"),   precision=0)
    _row("Avg ffmpeg",    a.get("avg_ffmpeg"),     b.get("avg_ffmpeg"))
    _row("Avg SRS CPU",   a.get("avg_srs_cpu"),    b.get("avg_srs_cpu"),   unit="%", delta=True)

    # Per-type ffmpeg breakdown (only if at least one run has it)
    types_a = a.get("ffmpeg_by_type", {})
    types_b = b.get("ffmpeg_by_type", {})
    all_types = sorted(set(list(types_a.keys()) + list(types_b.keys())))
    if all_types:
        _section("ffmpeg CPU by Source Type (avg % server CPU per stream)")
        for ft in all_types:
            ta = types_a.get(ft)
            tb = types_b.get(ft)
            va = f"{ta['count']}× @ {ta['avg_cpu_pct']:.2f}%" if ta else "—"
            vb = f"{tb['count']}× @ {tb['avg_cpu_pct']:.2f}%" if tb else "—"
            print(f"  {ft:<28} {va:<18} {vb}")

    print(f"\n{'─'*70}\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    compare(sys.argv[1], sys.argv[2])
