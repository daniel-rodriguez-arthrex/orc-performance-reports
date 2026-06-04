"""analyze_cpu.py — Quick CPU/RAM + ffmpeg PID analysis from a raw_data.json file.

Usage:
    python analyze_cpu.py                                  # uses default endurance run
    python analyze_cpu.py results/my_run/cap_val/raw_data.json
    python analyze_cpu.py results/my_run/cap_val/raw_data.json --stride 1
"""
import argparse
import json
import pathlib
import datetime
from collections import defaultdict


def _trim_stale_tail(points: list) -> list:
    """Remove repeated identical readings from the end (WinRM keepalive artifact)."""
    if not points:
        return points
    tail_cpu = points[-1].get("cpu_percent")
    tail_ram = points[-1].get("ram_pct")
    cut = len(points) - 1
    while cut > 0 and (
        points[cut - 1].get("cpu_percent") == tail_cpu
        and points[cut - 1].get("ram_pct") == tail_ram
    ):
        cut -= 1
    return points[:cut]


def _analyze_timeline(points: list, stride: int) -> None:
    hourly_cpu: dict = defaultdict(list)
    hourly_ram: dict = defaultdict(list)
    for p in points:
        h = int(p["elapsed_s"] // 3600)
        hourly_cpu[h].append(p.get("cpu_percent", 0))
        hourly_ram[h].append(p.get("ram_pct", 0))

    print(f'{"Hour":>5} | {"Avg CPU%":>8} | {"Avg RAM%":>8} | {"Max CPU%":>8}')
    print("-" * 40)
    for h in sorted(hourly_cpu)[::stride]:
        cpus = hourly_cpu[h]
        rams = hourly_ram[h]
        print(
            f"  h{h:3d} | {sum(cpus)/len(cpus):8.1f}"
            f" | {sum(rams)/len(rams):8.1f}"
            f" | {max(cpus):8.1f}"
        )


def _analyze_ffmpeg_pids(spikes: list) -> None:
    ff = [s for s in spikes if "ffmpeg" in s.get("process", "").lower()]
    if not ff:
        print("No ffmpeg spike rows found.")
        return

    all_pids    = [s["pid"] for s in ff]
    unique_pids = set(all_pids)
    print(f"Total ffmpeg spike rows : {len(ff)}")
    print(f"Unique ffmpeg PIDs      : {len(unique_pids)}")

    times_pids = sorted((s["time"], s["pid"]) for s in ff)
    seen: set = set()
    day_counts: dict = {}
    for t, pid in times_pids:
        seen.add(pid)
        try:
            day_counts[datetime.datetime.fromisoformat(t).date()] = len(seen)
        except ValueError:
            pass

    print()
    print("Cumulative unique ffmpeg PIDs per day:")
    for d, cnt in sorted(day_counts.items()):
        print(f"  {d}: {cnt} PIDs")


def main() -> None:
    default_path = (
        "results/run_2026-05-21_endurance_5day/"
        "endurance_test_20260521_153954/raw_data.json"
    )
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", default=default_path,
                        help="Path to raw_data.json (default: latest endurance run)")
    parser.add_argument("--stride", type=int, default=6, metavar="N",
                        help="Print every Nth hour in the timeline table (default: 6)")
    args = parser.parse_args()

    p = pathlib.Path(args.path)
    if not p.exists():
        parser.error(f"File not found: {p}")

    data = json.loads(p.read_text(encoding="utf-8"))

    # --- Server timeline ---
    raw_timeline = data.get("server_timeline", [])
    if not raw_timeline:
        print("No server_timeline data in this file.")
    else:
        live = _trim_stale_tail(raw_timeline)
        print(f"Live data points: {len(live)} (trimmed {len(raw_timeline) - len(live)} stale)")
        print()
        _analyze_timeline(live, args.stride)

    # --- ffmpeg PID accumulation ---
    spikes = (data.get("server") or {}).get("process_spikes", [])
    if spikes:
        print()
        _analyze_ffmpeg_pids(spikes)


if __name__ == "__main__":
    main()

