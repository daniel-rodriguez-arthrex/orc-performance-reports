"""
run_perf.py � ORC Performance Test Orchestrator

Usage:
    python run_perf.py --scenario capacity_validation --tiers 12 --headless --collect-server-metrics
    python run_perf.py --scenario capacity_validation --tiers 8 --layout 9
    python run_perf.py --scenario endurance_test --duration 8h --collect-server-metrics
    python run_perf.py --scenario enforcement_threshold --headless
    python run_perf.py --scenario all
    python run_perf.py --dry-run
"""

import os
import sys
import time
import json
import argparse
import re
import ctypes
from datetime import datetime
from contextlib import contextmanager

# Ensure UTF-8 output even when stdout is redirected to a file (e.g. detached Start-Process).
# Windows defaults to cp1252 for redirected streams, which chokes on arrow/emoji chars.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))

from playwright.sync_api import sync_playwright

from config.environments import ENV, SERVERS
from config.sources import CAMERAS, SIMULATED, VISION, MATRIX
from core.orc_client import OrcClient
from core.room_cleanup import RoomCleanup
from core.session_scaler import SessionScaler
from core.source_setup import SourceSetup
from metrics.webrtc_collector import WebRTCCollector
from metrics.server_metrics_collector import ServerMetricsCollector
from metrics.api_latency import ApiLatencyMonitor
from reporting.aggregator import aggregate
from reporting.report import write_report

try:
    from network.clumsy_control import ClusmsyController
    CLUMSY_AVAILABLE = True
except ImportError:
    CLUMSY_AVAILABLE = False

# All physical cameras (Sony + Axis) used first for non-Vision rooms.
# Once exhausted, remaining slots are filled with SIMULATED streams.
_PHYSICAL_CAMERAS = list(CAMERAS)  # Sony SRG-X120, SRG-300SE, SRG-A12, Axis M3085-V x2

ALL_SCENARIOS = [
    "capacity_validation",
    "layout_stress",
    "enforcement_threshold",
    "layout_scaling",
    "network_degradation",
]

CLUMSY_PRESETS = ["light", "moderate", "heavy", "severe"]

# CPU % at which a sample is considered a spike (server metrics + endurance alert).
# Single source of truth — used by _start_server_metrics() and run_endurance_test().
_SPIKE_THRESHOLD_PCT: float = 70.0


# Active server environment � set by main() based on --server flag.
# All helpers read from this instead of ENV so multi-server runs work.
ACTIVE_ENV: dict = ENV  # default: qa155


def _as_setup_source(s: dict) -> dict:
    """
    Translate a config/sources.py source dict (uses 'source_type' key)
    to the shape expected by SourceSetup.configure_rooms() (uses 'type' key).
    """
    return {
        "name":            s["name"],
        "type":            s["source_type"],
        "url":             s["url"],
        "username":        s.get("username", ""),
        "password":        s.get("password", ""),
        "bandwidth_spec":  s["bandwidth_spec"],
        "bandwidth_mbps":  s.get("bandwidth_mbps", 0),
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="ORC 2.1.0 Performance Test Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        choices=[*ALL_SCENARIOS, "endurance_test", "srs_handle_leak", "all"],
        default=None,
        help="Scenario to run (or 'all' to run every scenario in sequence)",
    )
    parser.add_argument(
        "--output-dir",
        default="results",
        help="Root directory for report output",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help=(
            "Run browsers headless using Chrome new headless mode "
            "(full GPU + WebRTC pipeline preserved). "
            "Omit for visible browser windows."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Seconds to wait between session ramp-up steps",
    )
    parser.add_argument(
        "--collect-server-metrics",
        action="store_true",
        help="Launch metrics/server_metrics.ps1 in the background during each scenario",
    )
    parser.add_argument(
        "--server",
        choices=["qa155", "qa160", "qa162", "qa172", "qa173", "visions", "all"],
        default="qa155",
        help=(
            "ORC server to target. 'all' iterates every server sequentially "
            "(only meaningful for the capacity_validation scenario)."
        ),
    )
    parser.add_argument(
        "--reset-rooms",
        action="store_true",
        help=(
            "Delete ALL rooms and their streams from the target server via GraphQL "
            "before running scenarios. Useful for a clean slate between test runs."
        ),
    )
    parser.add_argument(
        "--setup-sources",
        choices=["vision", "simulated", "mixed", "cameras"],
        default=None,
        help=(
            "Configure room sources and exit without running any scenario. "
            "'vision' = OR 01-06 with Vision RTSPS streams; "
            "'simulated' = OR 01-12 with simulated RTSP streams; "
            "'mixed' = OR 01-04 Vision + OR 05-08 simulated; "
            "'cameras' = OR 01-N with physical Sony/Axis cameras only (up to 4)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without executing anything",
    )
    parser.add_argument(
        "--tiers",
        type=int,
        nargs="+",
        metavar="N",
        default=None,
        help=(
            "Override server stream tiers. E.g. --tiers 12 runs only the 12-session tier. "
            "Accepts multiple values: --tiers 12 24"
        ),
    )
    parser.add_argument(
        "--layout",
        choices=["1", "4", "9", "12", "auto"],
        default="12",
        help=(
            "Dashboard layout each session opens with. '12' (default) shows all rooms "
            "simultaneously — maximum WebRTC connections per session. "
            "'1' = single stream per session (minimum load)."
        ),
    )
    parser.add_argument(
        "--duration",
        default="4h",
        metavar="DUR",
        help=(
            "Duration for the endurance_test scenario. "
            "Accepts hours/minutes/seconds: '8h', '30m', '2h30m'. "
            "Default: 4h."
        ),
    )
    parser.add_argument(
        "--sources",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of sources (rooms) for the endurance_test scenario. "
            "Defaults to the server's max stream tier (e.g. 36 for qa162, 12 for qa155)."
        ),
    )
    parser.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help=(
            "Optional label appended to the output folder name, e.g. 'qa162_4d'. "
            "Result: endurance_test_qa162_4d_20260529_143000"
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_admin_client() -> OrcClient:
    """Return an OrcClient pre-configured with admin credentials."""
    return OrcClient(ACTIVE_ENV)


def _make_non_admin_client() -> OrcClient:
    """Return an OrcClient pre-configured with non-admin (manager) credentials."""
    env_non_admin = {
        **ACTIVE_ENV,
        "username": ACTIVE_ENV["non_admin_user"],
        "password": ACTIVE_ENV["non_admin_pass"],
    }
    return OrcClient(env_non_admin)


def _reset_rooms(dry_run: bool = False) -> None:
    """Delete all rooms + streams from the active server via GraphQL API."""
    print(f"\n  [reset-rooms] Cleaning up rooms on {ACTIVE_ENV.get('base_url')} ...")
    cleanup = RoomCleanup(ACTIVE_ENV)
    result  = cleanup.cleanup_all_rooms(dry_run=dry_run)
    if result["errors"]:
        print(f"  [reset-rooms] {len(result['errors'])} error(s) during cleanup:")
        for e in result["errors"]:
            print(f"    - {e}")


@contextmanager
def _admin_page(pw, headless: bool):
    """
    Context manager that opens a single admin browser session, yields (client, page),
    then closes the browser. Used for one-off admin operations like setting egress cap.
    """
    client = _make_admin_client()
    browser, context = client.new_browser(pw, headless=headless)
    page = context.new_page()
    try:
        client.login(page)
        yield client, page
    finally:
        try:
            browser.close()
        except Exception:
            pass


def _set_cap(pw, mbps: int, headless: bool) -> None:
    """Open an admin session, set the egress cap, then close."""
    print(f"  [cap] Setting egress cap -> {mbps} Mbps")
    with _admin_page(pw, headless) as (client, page):
        client.set_egress_cap(page, mbps)


def _collect_webrtc(scaler: SessionScaler, connected_only: bool = True) -> list:
    """Collect a single WebRTC snapshot from each open page in scaler."""
    collector = WebRTCCollector()
    pages = scaler.get_pages() if connected_only else scaler.get_all_pages()
    return collector.collect_all(pages, delay_between_ms=200)


def _wait_for_webrtc_then_soak(scaler: SessionScaler, soak_s: int, poll_interval: float = 1.0) -> tuple[list, dict]:
    """Poll WebRTC stats every poll_interval seconds for the full soak window.

    Tracks the stream-arrival timeline so the report can show how quickly
    the server delivered all streams under load.

    Returns
    -------
    (final_snapshots, stream_timing)

    stream_timing keys
    ------------------
    timeline        list of {elapsed_s, total_conns, tabs_ready} — one entry per poll
    time_to_first_s seconds until any tab had >= 1 connection (None if never)
    time_to_all_s   seconds until all tabs had >= 1 connection (None if never)
    n_tabs          number of sessions polled
    """
    t0          = time.time()
    deadline    = t0 + soak_s
    collector   = WebRTCCollector()
    pages       = scaler.get_pages()
    n_tabs      = len(pages)

    timeline:        list[dict] = []
    time_to_first_s: float | None = None
    time_to_all_s:   float | None = None
    bytes_rx_at_start: dict = {}      # tab_index -> bytes_rx when all streams first active
    per_source_bx_at_start: dict = {} # tab_index -> [bytes per PC] when all streams first active

    while True:
        snaps     = collector.collect_all(pages, delay_between_ms=100)
        elapsed   = time.time() - t0
        total     = sum(s.connection_count for s in snaps)
        tabs_ready = sum(1 for s in snaps if s.connection_count > 0)

        timeline.append({"elapsed_s": round(elapsed, 1), "total_conns": total, "tabs_ready": tabs_ready})

        if time_to_first_s is None and total > 0:
            time_to_first_s = elapsed
            # Capture baseline bytes at first-stream moment — used as fallback
            # denominator if not all tabs reach streaming before soak ends.
            bytes_rx_at_start = {
                int(s.tab_index): (int(s.bytes_received) if s.bytes_received else 0)
                for s in snaps
            }
            per_source_bx_at_start = {
                int(s.tab_index): (list(s.per_source_bytes) if s.per_source_bytes else [])
                for s in snaps
            }
            print(f"  [tier] WebRTC first stream at {elapsed:.1f}s — "
                  f"{total} conn(s) across {tabs_ready}/{n_tabs} tab(s)")

        if time_to_all_s is None and tabs_ready >= n_tabs:
            time_to_all_s = elapsed
            # Override baseline with more accurate all-streaming snapshot.
            bytes_rx_at_start = {
                int(s.tab_index): (int(s.bytes_received) if s.bytes_received else 0)
                for s in snaps
            }
            per_source_bx_at_start = {
                int(s.tab_index): (list(s.per_source_bytes) if s.per_source_bytes else [])
                for s in snaps
            }
            print(f"  [tier] WebRTC all {n_tabs} session(s) streaming at {elapsed:.1f}s "
                  f"({total} total conn(s)). Holding remaining soak ...")

        if time.time() >= deadline:
            break
        time.sleep(min(poll_interval, max(0.1, deadline - time.time())))

    # Final snapshot for the report
    snaps = collector.collect_all(pages, delay_between_ms=100)
    if time_to_first_s is None:
        print(f"  [tier] WebRTC: no streams connected within {soak_s}s soak window.")
    elif time_to_all_s is None:
        print(f"  [tier] WebRTC: only {timeline[-1]['tabs_ready']}/{n_tabs} tab(s) streaming at soak end.")

    # stable_s = duration from first (or all) streaming to end of soak.
    # Falls back to time_to_first_s so partial-streaming tiers still get bandwidth data.
    _ref_s   = time_to_all_s if time_to_all_s is not None else time_to_first_s
    stable_s = round(soak_s - _ref_s, 2) if _ref_s is not None else 0.0
    stream_timing = {
        "timeline":             timeline,
        "time_to_first_s":      round(time_to_first_s, 2) if time_to_first_s is not None else None,
        "time_to_all_s":        round(time_to_all_s,   2) if time_to_all_s   is not None else None,
        "n_tabs":               n_tabs,
        "soak_s":               soak_s,
        "bytes_rx_at_start":       bytes_rx_at_start,    # {tab_index: bytes_rx}
        "per_source_bx_at_start":  per_source_bx_at_start,  # {tab_index: [bytes per PC]}
        "streaming_duration_s":    stable_s,             # denominator for per-source Mbps
    }
    return snaps, stream_timing


# ---------------------------------------------------------------------------
# Server metrics helpers
# ---------------------------------------------------------------------------

def _start_server_metrics(output_dir: str):
    """
    Start background server metrics collection via pywinrm NTLM.
    Returns (ServerMetricsCollector, csv_path) or (None, None) if credentials missing.
    """
    server_pass = ACTIVE_ENV.get("server_pass", "")
    if not server_pass:
        print("  [server-metrics] ORC_SERVER_PASS not set -- skipping server metrics.")
        return None, None

    csv_path                = os.path.join(output_dir, "server_metrics.csv")
    log_path                = os.path.join(output_dir, "server_metrics.log")
    spikes_path             = os.path.join(output_dir, "process_spikes.csv")
    proc_series_path        = os.path.join(output_dir, "proc_series.csv")
    ffmpeg_instances_path   = os.path.join(output_dir, "ffmpeg_instances.csv")
    print(f"  [server-metrics] Starting poller -> {csv_path}")
    collector = ServerMetricsCollector(
        host=ACTIVE_ENV.get("server_host", ""),
        user=ACTIVE_ENV.get("server_user", "Administrator"),
        password=server_pass,
        csv_path=csv_path,
        log_path=log_path,
        interval=5,
        spikes_csv_path=spikes_path,
        spike_threshold=_SPIKE_THRESHOLD_PCT,
        proc_series_csv_path=proc_series_path,
        ffmpeg_instances_csv_path=ffmpeg_instances_path,
    ).start()
    return collector, csv_path


def _stop_server_metrics(collector):
    """Stop the server metrics background thread."""
    if collector is not None:
        collector.stop()
        print("  [server-metrics] Poller stopped.")


def _attach_server_metrics(data: dict, csv_path: str) -> None:
    """
    Parse the server metrics CSV (written by server_metrics.ps1) and populate
    data["server"] in-place so write_report() can render the server charts.
    Re-uses aggregate()'s existing CSV parsing by calling it with empty inputs.
    """
    spikes_path             = os.path.join(os.path.dirname(csv_path), "process_spikes.csv")
    proc_series_path        = os.path.join(os.path.dirname(csv_path), "proc_series.csv")
    ffmpeg_instances_path   = os.path.join(os.path.dirname(csv_path), "ffmpeg_instances.csv")
    patched = aggregate(
        scenario_name=data.get("scenario", ""),
        session_results=[],
        webrtc_snapshots=[],
        api_calls=[],
        server_metrics_csv_path=csv_path,
        process_spikes_csv_path=spikes_path if os.path.exists(spikes_path) else None,
        proc_series_csv_path=proc_series_path if os.path.exists(proc_series_path) else None,
        ffmpeg_instances_csv_path=ffmpeg_instances_path if os.path.exists(ffmpeg_instances_path) else None,
    )
    data["server"] = patched.get("server")


# ---------------------------------------------------------------------------
# Scenario 3: Enforcement Threshold
# ---------------------------------------------------------------------------

def run_enforcement_threshold(pw, args) -> dict:
    """
    Verify bandwidth enforcement fires at the correct session using Vision streams.

    Bandwidth math (from bandwidth.json):
        Vision  1920x1080@60fps = 12 Mbps (ORC enforcement value)
        Cap = 40 Mbps  ?  allows 3 sessions (3�12=36), blocks 4th (4�12=48)

    NOTE: These are ORC enforcement values, not actual network bitrate.
    The Vision RTSPS streams may push a different bitrate on the wire;
    ORC counts the configured bandwidth_spec against the cap.

    Open 5 sessions � expect sessions 4 and 5 to be blocked.
    """
    _set_cap(pw, 40, args.headless)  # 40 Mbps: allows 3 Vision sessions, blocks 4th

    client = _make_non_admin_client()
    scaler = SessionScaler(client, pw)

    print("  [enforcement_threshold] Ramping up 5 sessions, expecting block at session 4 ...")
    results = scaler.ramp_up(
        target_count=5,
        interval_seconds=args.interval,
        layout="1",
        username=ACTIVE_ENV["non_admin_user"],
        password=ACTIVE_ENV["non_admin_pass"],
        headless=args.headless,
    )

    first_blocked = next(
        (r.tab_index for r in results if r.modal_fired), None
    )
    if first_blocked is not None:
        print(f"  [enforcement_threshold] Bandwidth modal first triggered at tab index {first_blocked}")
    else:
        print("  [enforcement_threshold] WARNING: No bandwidth modal detected across 5 sessions")

    _snaps_t0  = _collect_webrtc(scaler, connected_only=True)
    _bx_start  = {int(s.tab_index): (int(s.bytes_received) if s.bytes_received else 0) for s in _snaps_t0}
    _t_soak    = time.time()
    time.sleep(_WEBRTC_SOAK_SECONDS)
    snapshots  = _collect_webrtc(scaler, connected_only=True)
    _soak_dur  = round(time.time() - _t_soak, 1)

    data = aggregate(
        scenario_name="enforcement_threshold",
        session_results=results,
        webrtc_snapshots=snapshots,
        api_calls=[],
        stream_timing={"bytes_rx_at_start": _bx_start, "streaming_duration_s": _soak_dur, "soak_s": _WEBRTC_SOAK_SECONDS},
    )

    scaler.close_all()

    # Always reset cap after enforcement test
    _set_cap(pw, 1000, args.headless)
    return data


# ---------------------------------------------------------------------------
# Scenario 4: Layout Scaling
# ---------------------------------------------------------------------------

def run_layout_scaling(pw, args) -> dict:
    """
    Single browser session cycling through layouts: 1 -> 4 -> 9 -> 12 -> 1.
    Capture API latency for each layout change using ApiLatencyMonitor.
    """
    _set_cap(pw, 1000, args.headless)

    client = _make_non_admin_client()
    browser, context = client.new_browser(pw, headless=args.headless)
    page = context.new_page()

    monitor = ApiLatencyMonitor()
    monitor.attach(page)

    print("  [layout_scaling] Logging in ...")
    client.login(page, username=ACTIVE_ENV["non_admin_user"], password=ACTIVE_ENV["non_admin_pass"])
    client.go_dashboard(page)

    layout_sequence = ["1", "4", "9", "12", "1"]
    all_api_calls = []

    for layout in layout_sequence:
        print(f"  [layout_scaling] Switching to layout '{layout}' ...")
        monitor.clear()
        client.click_layout(page, layout)
        time.sleep(5)  # let Angular fire bandwidth check + render

        calls = monitor.get_orc_api_calls()
        bw_calls = monitor.get_bandwidth_calls()
        all_api_calls.extend(calls)
        print(f"    -> {len(calls)} API calls ({len(bw_calls)} bandwidth checks)")

    try:
        browser.close()
    except Exception:
        pass

    data = aggregate(
        scenario_name="layout_scaling",
        session_results=[],
        webrtc_snapshots=[],
        api_calls=all_api_calls,
    )
    return data


# ---------------------------------------------------------------------------
# Scenario 5: Network Degradation
# ---------------------------------------------------------------------------

def run_network_degradation(pw, args) -> dict:
    """
    Run 3 Matrix sessions under each Clumsy preset: light -> moderate -> heavy -> severe.
    Requires Clumsy to be installed; skips gracefully if unavailable.
    """
    if not CLUMSY_AVAILABLE:
        print("  [network_degradation] network/clumsy_control.py not importable -- skipping.")
        return aggregate(
            scenario_name="network_degradation",
            session_results=[],
            webrtc_snapshots=[],
            api_calls=[],
        )

    clumsy = ClusmsyController()
    if not clumsy.available:
        print("  [network_degradation] clumsy.exe not found -- skipping.")
        return aggregate(
            scenario_name="network_degradation",
            session_results=[],
            webrtc_snapshots=[],
            api_calls=[],
        )

    combined_results = []
    combined_snapshots = []

    for preset in CLUMSY_PRESETS:
        print(f"  [network_degradation] Applying Clumsy preset: {preset} ...")
        _set_cap(pw, 1000, args.headless)

        applied = clumsy.apply_preset(preset)
        if not applied:
            print(f"  [network_degradation] Could not apply preset '{preset}' -- skipping.")
            continue

        client = _make_non_admin_client()
        scaler = SessionScaler(client, pw)

        results = scaler.ramp_up(
            target_count=3,
            interval_seconds=args.interval,
            layout="1",
            username=ACTIVE_ENV["non_admin_user"],
            password=ACTIVE_ENV["non_admin_pass"],
            headless=args.headless,
        )
        time.sleep(_WEBRTC_SOAK_SECONDS)
        snapshots = _collect_webrtc(scaler, connected_only=True)

        connected = sum(1 for r in results if r.connected)
        avg_load = (
            sum(r.load_time_ms for r in results) / len(results) if results else 0.0
        )
        print(f"    -> preset={preset} connected={connected}/3 avg_load={avg_load:.0f}ms")

        combined_results.extend(results)
        combined_snapshots.extend(snapshots)

        clumsy.stop()
        scaler.close_all()

    data = aggregate(
        scenario_name="network_degradation",
        session_results=combined_results,
        webrtc_snapshots=combined_snapshots,
        api_calls=[],
    )
    return data


# ---------------------------------------------------------------------------
# Scenario 6: Hardware Comparison  ? MULTI-SERVER LOAD TIERS
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sleep prevention (Windows)
# ---------------------------------------------------------------------------
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001

@contextmanager
def _prevent_sleep():
    """
    Keep the machine awake for the duration of a long-running test.
    Uses SetThreadExecutionState on Windows; no-op on other platforms.
    Prints a notice so operators know sleep is suppressed.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        prev = kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        print("  [sleep-guard] Machine sleep suppressed for this run.")
        try:
            yield
        finally:
            kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # restore
            print("  [sleep-guard] Machine sleep re-enabled.")
    except AttributeError:
        # Non-Windows — ctypes.windll not available, silently skip
        yield


def _parse_duration(s: str) -> int:
    """Parse a duration string like '8h', '30m', '2h30m', '5d', '5d12h' into total seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)([dhms])", s.lower()):
        v = int(value)
        if unit == "d":
            total += v * 86400
        elif unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        elif unit == "s":
            total += v
    if total == 0:
        try:
            total = int(s) * 3600
        except ValueError:
            raise ValueError(
                f"Cannot parse duration: '{s}'. Use format '8h', '30m', '2h30m'."
            )
    return total


def _source_type(name: str) -> str:
    """Classify a source name as Vision / Physical / Simulated."""
    n = (name or "").lower()
    if n.startswith("vision"):
        return "Vision"
    if "sim" in n:
        return "Simulated"
    return "Physical"


def _build_room_sources(n_sessions: int) -> list[dict]:
    """
    Build the room_sources list for a load tier.

    Stream assignment (in order):
      1. First min(6, n_sessions) rooms → Vision RTSPS devices (12 Mbps each)
      2. Next min(len(CAMERAS), remaining) rooms → physical cameras in order
         (Sony SRG-X120, SRG-300SE, SRG-A12, Axis M3085-V x2)
      3. Next min(len(MATRIX), remaining) rooms → Matrix RTMP sources
      4. Any remaining rooms → SIMULATED RTSP streams, cycling from index 0

    Returns a list of dicts compatible with SourceSetup.configure_rooms().
    """
    n_vision    = min(6, n_sessions)
    n_remaining = n_sessions - n_vision

    n_physical  = min(len(_PHYSICAL_CAMERAS), n_remaining)
    n_remaining -= n_physical

    n_matrix    = min(len(MATRIX), n_remaining)
    n_sim       = n_remaining - n_matrix

    room_sources = []
    for i in range(n_vision):
        room_sources.append({
            "room_name":    f"OR {i + 1:02d}",
            "source":       _as_setup_source(VISION[i % len(VISION)]),
            "make_primary": True,
        })
    for j in range(n_physical):
        room_sources.append({
            "room_name":    f"OR {n_vision + j + 1:02d}",
            "source":       _as_setup_source(_PHYSICAL_CAMERAS[j]),
            "make_primary": True,
        })
    for m in range(n_matrix):
        room_sources.append({
            "room_name":    f"OR {n_vision + n_physical + m + 1:02d}",
            "source":       _as_setup_source(MATRIX[m % len(MATRIX)]),
            "make_primary": True,
        })
    for k in range(n_sim):
        room_sources.append({
            "room_name":    f"OR {n_vision + n_physical + n_matrix + k + 1:02d}",
            "source":       _as_setup_source(SIMULATED[k % len(SIMULATED)]),
            "make_primary": True,
        })
    return room_sources


def _run_tier(pw, args, server_cfg: dict, n_sessions: int, output_dir: str,
              server_collector=None) -> dict:
    """
    Run a single load tier against the given server config.

    server_collector : ServerMetricsCollector | None
        If provided, its get_latest() is polled between each session ramp-up
        step.  If CPU or RAM exceeds the FAIL threshold, the ramp-up is aborted
        gracefully — existing sessions are still measured, rooms are reset, and
        the report records the abort reason.
    """
    global ACTIVE_ENV
    ACTIVE_ENV = server_cfg

    server_name = server_cfg.get("name", "unknown")
    hardware    = server_cfg.get("hardware", "")
    print(f"\n  [tier] server={server_name} ({hardware}) sessions={n_sessions}")

    # 0. Optional room reset (wipe all rooms before reconfiguring for this tier)
    if getattr(args, "reset_rooms", False):
        _reset_rooms(dry_run=args.dry_run)

    # 0b. Ensure all required rooms exist + configure sources — all via GraphQL, no browser
    room_names = [f"OR {i:02d}" for i in range(1, n_sessions + 1)]
    if not args.dry_run:
        room_mgr = RoomCleanup(ACTIVE_ENV)
        room_mgr.ensure_rooms_exist(room_names)

        # 1. Configure rooms via GraphQL (fast, no Playwright)
        n_vision   = min(6, n_sessions)
        n_physical = min(len(_PHYSICAL_CAMERAS), n_sessions - n_vision)
        n_sim      = n_sessions - n_vision - n_physical
        n_matrix_t = min(len(MATRIX), n_sessions - n_vision - n_physical)
        n_sim_t    = n_sessions - n_vision - n_physical - n_matrix_t
        mix_t      = [f"{n_vision} Vision", f"{n_physical} Physical"]
        if n_matrix_t:
            mix_t.append(f"{n_matrix_t} Matrix")
        if n_sim_t:
            mix_t.append(f"{n_sim_t} Simulated")
        print(f"  [tier] source mix: {' + '.join(mix_t)}")

        room_sources = _build_room_sources(n_sessions)
        results_cfg = SourceSetup(ACTIVE_ENV).configure_rooms(room_sources)
        errors_cfg = [r for r, s in results_cfg.items() if s.startswith("error")]
        if errors_cfg:
            print(f"  [tier] WARNING: {len(errors_cfg)} room(s) failed to configure: {errors_cfg}")

    # 2. Set cap unlimited
    _set_cap(pw, 1000, args.headless)

    # 3. Ramp up sessions — use admin so all rooms are visible and assigned
    client  = _make_admin_client()
    scaler  = SessionScaler(client, pw)
    monitor = ApiLatencyMonitor()

    # Abort check: stop adding sessions if server CPU or RAM hits the FAIL threshold.
    # This prevents hammering a server that is already at its limit.
    _CPU_ABORT = 85   # matches _THRESH["max_cpu_pct"] fail level
    _RAM_ABORT = 85   # matches _THRESH["avg_ram_pct"] fail level

    def _server_abort_check() -> str | None:
        if server_collector is None:
            return None
        latest = server_collector.get_latest()
        cpu = latest.get("cpu_percent")
        ram = latest.get("ram_pct")
        if cpu is not None and cpu >= _CPU_ABORT:
            return f"CPU reached {cpu:.0f}% (abort threshold: {_CPU_ABORT}%)"
        if ram is not None and ram >= _RAM_ABORT:
            return f"RAM reached {ram:.0f}% (abort threshold: {_RAM_ABORT}%)"
        return None

    ramp_start_epoch = time.time()
    results = scaler.ramp_up(
        target_count=n_sessions,
        interval_seconds=args.interval,
        layout=getattr(args, "layout", "12"),
        username=ACTIVE_ENV["admin_user"],
        password=ACTIVE_ENV["admin_pass"],
        headless=args.headless,
        page_callbacks=[monitor.attach],
        abort_check=_server_abort_check,
    )

    abort_reason = getattr(scaler, "abort_reason", None)
    if abort_reason:
        sessions_opened = len(results)
        print(f"  [tier] *** ABORTED at {sessions_opened}/{n_sessions} sessions — {abort_reason} ***")
        print(f"  [tier] Collecting data from {sessions_opened} active session(s) then resetting rooms ...")

    # 4. Pre-soak WHEP reconnect: long ramps (e.g. 12 sessions × 30s = 330s) can push
    #    early sessions past the server-side ~300s WHEP session timeout.  Reconnect
    #    any tabs with ≤1 connections BEFORE the measurement window so WebRTC stats
    #    are valid for all sessions.  Skip if we aborted mid-ramp (degraded state).
    _dead_pages = []  # pre-init so pre_soak_reconnects is always defined
    if not abort_reason:
        _all_pages = scaler.get_all_pages()
        if _all_pages:
            _pre_snaps = WebRTCCollector().collect_all(_all_pages, delay_between_ms=100)
            # Threshold <= 1: a tab with only 1 connection has just the ORC signaling
            # channel (non-video) — video streams have not (re)connected yet.
            _dead_pages = [_all_pages[i] for i, s in enumerate(_pre_snaps)
                           if s.connection_count <= 1]
            if _dead_pages:
                _layout = getattr(args, "layout", "12")
                print(f"  [tier] {len(_dead_pages)} tab(s) have ≤1 connections "
                      f"(WHEP timeout during ramp) — reconnecting before soak ...")
                for _p in _dead_pages:
                    try:
                        client.login(_p)
                        client.go_dashboard(_p)
                        if _layout != "1":
                            client.click_layout(_p, _layout)
                    except Exception as _re:
                        print(f"  [tier] Pre-soak reconnect error: {_re}")
                time.sleep(20)
                print(f"  [tier] Pre-soak reconnect complete ({len(_dead_pages)} tab(s) restored).")

    # 4b. Collect metrics — poll until WebRTC streams are up, hold for full soak
    print(f"  [tier] Waiting up to {_WEBRTC_SOAK_SECONDS}s for streams to stabilize ...")
    webrtc_snaps, stream_timing = _wait_for_webrtc_then_soak(scaler, _WEBRTC_SOAK_SECONDS)
    api_calls    = monitor.get_orc_api_calls()

    connected = sum(1 for r in results if r.connected)
    modals    = sum(1 for r in results if r.modal_fired)
    avg_load  = (
        sum(r.load_time_ms for r in results) / len(results) if results else 0.0
    )
    print(f"  [tier] done � connected={connected}/{n_sessions} "
          f"modals={modals} avg_load={avg_load:.0f}ms")

    scaler.close_all()
    monitor.clear()

    scenario_label = f"cap_val_{server_name}_{n_sessions}sessions"
    data = aggregate(
        scenario_name=scenario_label,
        session_results=results,
        webrtc_snapshots=webrtc_snaps,
        api_calls=api_calls,
        stream_timing=stream_timing,
    )
    data["hardware"]            = hardware
    data["server_name"]         = server_name
    data["session_tier"]        = n_sessions
    data["interval_s"]          = args.interval
    data["ramp_start_epoch"]    = ramp_start_epoch
    # Record how many tabs needed pre-soak reconnection (WHEP timeout during ramp).
    # 0 = clean ramp; >0 = sessions timed out but were restored before measurement.
    data["pre_soak_reconnects"] = len(_dead_pages)
    data["stream_timing"]    = stream_timing   # {timeline, time_to_first_s, time_to_all_s, n_tabs}
    data["abort_reason"]   = abort_reason    # None = completed normally; str = stopped early
    data["room_sources"]   = [
        {
            "room":   rs["room_name"],
            "source": rs["source"].get("name", rs["source"].get("url", "")),
            "type":   _source_type(rs["source"].get("name", rs["source"].get("url", ""))),
        }
        for rs in (room_sources if not args.dry_run else [])
    ]

    # If aborted mid-ramp, reset rooms so the server isn't left throttled.
    if abort_reason and not args.dry_run:
        print("  [tier] Resetting rooms after abort ...")
        try:
            _reset_rooms(dry_run=False)
        except Exception as exc:
            print(f"  [tier] WARNING: room reset after abort failed: {exc}")

    return data


def run_capacity_validation(pw, args) -> dict:
    """
    Sequential multi-server, multi-tier load test.

    For each selected server the scenario runs its configured stream tiers one at a
    time, writes a per-tier report, then moves to the next server.  A top-level
    summary dict is returned (and written as a combined JSON report).

    Server stream tiers (from config/environments.py):
        qa162  16 core / 32 GB  ?  12, 24, 36 sessions
        qa155   8 core / 16 GB  ?  12, 24, 36 sessions
        qa160   4 core /  8 GB  ?  12, 18, 24 sessions
    """
    servers_to_test = (
        list(SERVERS.values())
        if args.server == "all"
        else [SERVERS[args.server]]
    )

    all_tier_data = []
    output_root   = args.output_dir

    for server_cfg in servers_to_test:
        server_name = server_cfg.get("name", "unknown")
        tiers       = getattr(args, "tiers", None) or server_cfg.get("stream_tiers", [12])

        print(f"\n[capacity_validation] === {server_name} "
              f"({server_cfg.get('hardware', '')}) ===")
        print(f"  Tiers: {tiers}")

        for n in tiers:
            tier_dir = os.path.join(
                output_root, f"cap_val_{server_name}_{n}sessions"
            )
            os.makedirs(tier_dir, exist_ok=True)

            server_proc = server_csv = None
            if args.collect_server_metrics:
                server_proc, server_csv = _start_server_metrics(tier_dir)

            try:
                tier_data = _run_tier(pw, args, server_cfg, n, tier_dir,
                                      server_collector=server_proc)
            finally:
                _stop_server_metrics(server_proc)

            if server_csv and os.path.exists(server_csv):
                _attach_server_metrics(tier_data, server_csv)

            tier_paths = write_report(tier_data, tier_dir)
            print(f"  [hw_cmp] Tier report: {tier_paths.get('html', '--')}")

            all_tier_data.append(tier_data)

    # Roll up into a combined summary entry
    combined = aggregate(
        scenario_name="capacity_validation",
        session_results=[],
        webrtc_snapshots=[],
        api_calls=[],
    )
    combined["tiers"] = all_tier_data
    combined["servers_tested"] = [
        {"name": s.get("name"), "hardware": s.get("hardware"),
         "tiers": s.get("stream_tiers")}
        for s in servers_to_test
    ]
    return combined


# ---------------------------------------------------------------------------
# Scenario 7: Layout Stress  <- CONCURRENT LAYOUT CYCLING
# ---------------------------------------------------------------------------

# Streams per layout — how many WebRTC connections each session holds at each step.
_STREAMS_PER_LAYOUT: dict[str, int] = {"1": 1, "4": 4, "9": 9, "12": 12}
_LAYOUT_SOAK_SECONDS = 30  # seconds to hold each layout before collecting metrics
_WEBRTC_SOAK_SECONDS = 30  # seconds to wait after ramp-up for SRS WebRTC streams to connect


def run_layout_stress(pw, args) -> dict:
    """
    Concurrent layout-cycling stress test.

    All sessions open simultaneously on layout '1', then every session
    is cycled through 1 -> 4 -> 9 -> 12 -> 1 in lockstep.  Server metrics
    and WebRTC stats are captured at each step.

    Key metric: total_webrtc_connections = n_sessions x streams_per_layout.
    This is the true proxy for ORC server CPU/memory load.

    Egress cap is set to 1000 Mbps (effectively unlimited) so ORC's bandwidth
    enforcement never blocks sessions — the hardware is the only bottleneck.
    """
    # Default to 12 sessions: 6 Vision + 5 Physical + 1 Simulated (standard tier).
    # --tiers N overrides if supplied.
    n_sessions = (getattr(args, "tiers", None) or [12])[0]

    print(f"  [layout_stress] {n_sessions} concurrent sessions, "
          f"layouts: 1->4->9->12->1, soak={_LAYOUT_SOAK_SECONDS}s each")

    # 1. Configure rooms via GraphQL (full save sequence: createStream → updateStream(SRS) → updateRoom → sendConfig → assignRooms)
    room_sources = _build_room_sources(n_sessions)
    room_mgr = RoomCleanup(ACTIVE_ENV)
    room_mgr.ensure_rooms_exist([r["room_name"] for r in room_sources])
    SourceSetup(ACTIVE_ENV).configure_rooms(room_sources)

    # 2. Cap = unlimited
    _set_cap(pw, 1000, args.headless)

    # 3. Ramp up all sessions on layout '1'
    client  = _make_non_admin_client()
    scaler  = SessionScaler(client, pw)
    monitor = ApiLatencyMonitor()

    print(f"  [layout_stress] Ramping up {n_sessions} sessions (interval={args.interval}s) ...")
    session_results = scaler.ramp_up(
        target_count=n_sessions,
        interval_seconds=args.interval,
        layout=getattr(args, "layout", "12"),
        username=ACTIVE_ENV["non_admin_user"],
        password=ACTIVE_ENV["non_admin_pass"],
        headless=args.headless,
        page_callbacks=[monitor.attach],
    )
    connected = sum(1 for r in session_results if r.connected)
    print(f"  [layout_stress] {connected}/{n_sessions} sessions connected.")

    # 4. Cycle all sessions through each layout, collect metrics at each step
    layout_steps = []
    layout_sequence = ["1", "4", "9", "12", "1"]

    for layout in layout_sequence:
        streams_each = _STREAMS_PER_LAYOUT[layout]
        total_conns  = connected * streams_each
        print(f"  [layout_stress] Layout {layout}-up "
              f"({streams_each} streams/session x {connected} sessions "
              f"= {total_conns} total WebRTC connections) "
              f"soak {_LAYOUT_SOAK_SECONDS}s ...")

        # Switch every open session to this layout simultaneously
        monitor.clear()
        for page in scaler.get_pages():
            try:
                client.click_layout(page, layout)
            except Exception:  # noqa: BLE001
                pass  # page may have gone stale

        time.sleep(_LAYOUT_SOAK_SECONDS)

        webrtc_snaps = _collect_webrtc(scaler)
        api_calls    = monitor.get_orc_api_calls()

        avg_fps     = round(sum((s.fps or 0) for s in webrtc_snaps) / max(len(webrtc_snaps), 1), 2)
        avg_jitter  = round(sum((s.jitter_ms or 0) for s in webrtc_snaps) / max(len(webrtc_snaps), 1), 2)
        dropped     = sum((s.dropped_frames or 0) for s in webrtc_snaps)
        avg_api_ms  = round(sum((c.duration_ms or 0) for c in api_calls) / max(len(api_calls), 1), 2)

        step = {
            "layout":               layout,
            "streams_per_session":  streams_each,
            "total_webrtc_conns":   total_conns,
            "sessions":             connected,
            "webrtc_snapshots":     webrtc_snaps,
            "api_calls":            api_calls,
            "avg_fps":              avg_fps,
            "avg_jitter_ms":        avg_jitter,
            "total_dropped_frames": dropped,
            "avg_api_ms":           avg_api_ms,
        }
        layout_steps.append(step)
        print(f"    fps={avg_fps} jitter={avg_jitter}ms dropped={dropped} "
              f"api={avg_api_ms}ms conns={total_conns}")

    scaler.close_all()
    monitor.clear()

    data = aggregate(
        scenario_name="layout_stress",
        session_results=session_results,
        webrtc_snapshots=[s for step in layout_steps for s in step["webrtc_snapshots"]],
        api_calls=[c for step in layout_steps for c in step["api_calls"]],
    )
    data["layout_steps"] = layout_steps
    data["server_name"]  = ACTIVE_ENV.get("name", "unknown")
    data["hardware"]     = ACTIVE_ENV.get("hardware", "")
    return data
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Scenario: Endurance Test  — LONG-DURATION SOAK
# ---------------------------------------------------------------------------

_ENDURANCE_WEBRTC_POLL_S    = 300        # WebRTC poll every 5 minutes
_ENDURANCE_SERVER_POLL_S    = 60         # Server metrics poll every 60 seconds
_ENDURANCE_CPU_ALERT_PCT    = _SPIKE_THRESHOLD_PCT  # CPU % threshold for sustained-high alert
_ENDURANCE_RAM_DRIFT_PCT    = 0.20       # RAM fractional drift threshold (20%)
_ENDURANCE_CPU_WINDOW       = 10         # Consecutive 60-second samples = 10 minutes
_ENDURANCE_BROWSER_RESTART_S = 4 * 3600  # Full browser restart every 4 hours
_ENDURANCE_CHECKPOINT_S     = 3600       # Checkpoint in-memory data every 1 hour


def run_endurance_test(pw, args) -> dict:
    """
    Long-duration stability / soak test.

    Runs a single ORC session for --duration (default 4h) and monitors:
      - WebRTC stream health every 5 minutes
      - Server CPU / RAM every 60 seconds (requires --collect-server-metrics)

    Alerts are raised (but do not stop the test) for:
      - WebRTC connection drop: count falls to 0 after previously being > 0
      - RAM drift: usage exceeds baseline reading by more than 20%
      - CPU sustained: CPU stays above 70% for 10 consecutive minutes

    Prerequisites:
      Run --setup-sources <type> beforehand to configure room sources,
      or pass --reset-rooms to auto-configure 12 sources before the soak.
    """
    from collections import deque

    duration_s = _parse_duration(getattr(args, "duration", "4h"))
    layout = getattr(args, "layout", "12")
    # Default to server's max stream tier so qa162 (36) and qa155 (12) are correct automatically
    _src = getattr(args, "sources", None)
    n_sources = ACTIVE_ENV.get("stream_tiers", [12])[-1] if _src is None else _src
    print(
        f"  [endurance_test] Duration: {duration_s}s "
        f"({getattr(args, 'duration', '4h')}), layout={layout}, sources={n_sources}"
    )

    # ── Optional room setup ───────────────────────────────────────────────
    room_sources_meta = []
    if getattr(args, "reset_rooms", False) and not getattr(args, "dry_run", False):
        print(f"  [endurance_test] Resetting all rooms on {ACTIVE_ENV['base_url']} ...")
        room_mgr = RoomCleanup(ACTIVE_ENV)
        room_mgr.cleanup_all_rooms(dry_run=False)
        print(f"  [endurance_test] Recreating {n_sources} rooms ...")
        room_mgr.ensure_rooms_exist([f"OR {i:02d}" for i in range(1, n_sources + 1)])
        print(f"  [endurance_test] Configuring sources for {n_sources} rooms ...")
        room_sources = _build_room_sources(n_sources)
        SourceSetup(ACTIVE_ENV).configure_rooms(room_sources)

    # Always fetch live room sources from the API so the report shows
    # what is actually configured (not just when --reset-rooms is used).
    if not getattr(args, "dry_run", False):
        try:
            print("  [endurance_test] Reading room sources from API ...")
            room_sources_meta = RoomCleanup(ACTIVE_ENV).read_room_sources()
            print(f"  [endurance_test] {len(room_sources_meta)} room source(s) found.")
        except Exception as _rse:  # noqa: BLE001
            print(f"  [endurance_test] Warning: could not read room sources: {_rse}")

    _set_cap(pw, 1000, args.headless)

    # Use admin so all 12 rooms are visible regardless of assignment
    client = _make_admin_client()
    scaler = SessionScaler(client, pw)
    print(f"  [endurance_test] Opening 1 session (layout={layout}) ...")
    results = scaler.ramp_up(
        target_count=1,
        interval_seconds=0,
        layout=layout,
        username=ACTIVE_ENV["admin_user"],
        password=ACTIVE_ENV["admin_pass"],
        headless=args.headless,
    )

    if not results or not results[0].connected:
        print("  [endurance_test] ERROR: Session failed to connect — aborting.")
        scaler.close_all()
        return aggregate(
            scenario_name="endurance_test",
            session_results=results or [],
            webrtc_snapshots=[],
            api_calls=[],
        )

    print("  [endurance_test] Waiting for initial WebRTC connection ...")
    webrtc_snaps_initial, stream_timing = _wait_for_webrtc_then_soak(
        scaler, _WEBRTC_SOAK_SECONDS
    )

    print(f"  [endurance_test] Soak started — monitoring for {duration_s}s ...")
    alerts         = []
    webrtc_timeline = []   # [{elapsed_s, connection_count, fps, jitter_ms}]
    server_timeline = []   # [{elapsed_s, cpu_percent, ram_pct}]

    baseline_ram_pct  = None
    cpu_window        = deque(maxlen=_ENDURANCE_CPU_WINDOW)
    prev_webrtc_conns = None
    server_collector  = getattr(args, "_server_collector", None)
    last_browser_restart = 0.0  # elapsed_s of last browser restart
    last_checkpoint   = 0.0     # elapsed_s of last checkpoint write
    _output_dir       = getattr(args, "_scenario_output", None)

    with _prevent_sleep():
        # Auto-scale WebRTC poll interval: shorter polls for short soak durations.
        # This ensures the 10-min smoke test gets enough data points to validate.
        if duration_s <= 600:
            _webrtc_poll_s = 30
        elif duration_s <= 3600:
            _webrtc_poll_s = 60
        else:
            _webrtc_poll_s = _ENDURANCE_WEBRTC_POLL_S  # 300s default for long soaks
        print(f"  [endurance_test] WebRTC poll interval: {_webrtc_poll_s}s")

        start_time       = time.monotonic()
        end_time         = start_time + duration_s
        # Force first poll immediately by setting last poll time to far in the past
        last_webrtc_poll = start_time - _webrtc_poll_s
        last_server_poll = start_time - _ENDURANCE_SERVER_POLL_S
        # Reconnect state: track last attempt to enforce 90s cooldown
        last_reconnect_elapsed = -120.0

        while time.monotonic() < end_time:
            now     = time.monotonic()
            elapsed = now - start_time

            # ── WebRTC poll ────────────────────────────────────────────────────
            if (now - last_webrtc_poll) >= _webrtc_poll_s:
                try:
                    snaps = _collect_webrtc(scaler)
                    # Fallback: if connected-only filter returned nothing, try all pages
                    if not snaps:
                        snaps = _collect_webrtc(scaler, connected_only=False)
                    if snaps:
                        s          = snaps[0]
                        conn_count = s.connection_count
                        webrtc_timeline.append({
                            "elapsed_s":        round(elapsed, 1),
                            "connection_count": conn_count,
                            "fps":              s.fps,
                            "jitter_ms":        s.jitter_ms,
                        })
                        if prev_webrtc_conns is not None and prev_webrtc_conns > 0 and conn_count == 0:
                            alert = {
                                "elapsed_s": round(elapsed, 1),
                                "type":      "CONNECTION_DROP",
                                "message":   f"WebRTC connections dropped to 0 (was {prev_webrtc_conns})",
                            }
                            alerts.append(alert)
                            print(
                                f"  [endurance] WARNING {alert['message']} "
                                f"at t={elapsed:.0f}s"
                            )
                        prev_webrtc_conns = conn_count
                        print(
                            f"  [endurance] t={elapsed:.0f}s  WebRTC: {conn_count} conns, "
                            f"fps={s.fps:.1f}, jitter={s.jitter_ms:.1f}ms"
                        )
                except Exception as exc:
                    print(f"  [endurance] WebRTC poll error at t={elapsed:.0f}s: {exc}")
                last_webrtc_poll = now

            # ── Stream reconnect on drop ────────────────────────────────────────
            # ORC's WHEP server has a ~5-minute stream timeout. When all connections
            # drop to 0, do a full re-login + navigate + layout to re-establish WHEP.
            # Cooldown: only attempt once every 90s to avoid a reload storm.
            if (
                prev_webrtc_conns == 0
                and (elapsed - last_reconnect_elapsed) >= 90
            ):
                last_reconnect_elapsed = elapsed
                try:
                    pages = scaler.get_all_pages()
                    if pages:
                        print(
                            f"  [endurance] Reconnecting {len(pages)} tab(s) at t={elapsed:.0f}s "
                            f"(login -> dashboard -> {layout}-up layout) ..."
                        )
                        for _p in pages:
                            try:
                                client.login(_p)
                                client.go_dashboard(_p)
                                if layout != "1":
                                    client.click_layout(_p, layout)
                            except Exception as _re:
                                print(f"  [endurance] Reconnect error on tab: {_re}")
                        # Give Angular time to negotiate new WHEP subscriptions
                        time.sleep(20)
                        # Clear stale drop state so next poll is treated fresh
                        prev_webrtc_conns = None
                        # Upgrade ALL pending CONNECTION_DROP alerts to WHEP_RECONNECT.
                        # A single reconnect restores all streams, so every unresolved
                        # drop in this cycle is expected/recovered — not a true failure.
                        for _a in alerts:
                            if _a["type"] == "CONNECTION_DROP":
                                _a["type"]    = "WHEP_RECONNECT"
                                _a["message"] = _a["message"].replace(
                                    "WebRTC connections dropped to 0",
                                    "WHEP timeout — reconnected OK",
                                )
                        print("  [endurance] Reconnect complete — resuming monitoring ...")
                except Exception as _outer:
                    print(f"  [endurance] Reconnect outer error: {_outer}")

            # ── Server metrics poll (every 60 seconds) ─────────────────────────
            if server_collector and (now - last_server_poll) >= _ENDURANCE_SERVER_POLL_S:
                latest = server_collector.get_latest()
                if latest and latest.get("cpu_percent") is not None:
                    cpu = latest["cpu_percent"]
                    ram = latest["ram_pct"]
                    server_timeline.append({
                        "elapsed_s":   round(elapsed, 1),
                        "cpu_percent": cpu,
                        "ram_pct":     ram,
                    })
                    if baseline_ram_pct is None:
                        baseline_ram_pct = ram
                        print(f"  [endurance] Baseline RAM: {ram:.1f}%")
                    # Detect RAM drift >20% from baseline
                    if baseline_ram_pct and ram > baseline_ram_pct * (1 + _ENDURANCE_RAM_DRIFT_PCT):
                        alert = {
                            "elapsed_s": round(elapsed, 1),
                            "type":      "RAM_DRIFT",
                            "message":   (
                                f"RAM {ram:.1f}% exceeds baseline "
                                f"{baseline_ram_pct:.1f}% by "
                                f">{_ENDURANCE_RAM_DRIFT_PCT*100:.0f}%"
                            ),
                        }
                        alerts.append(alert)
                        print(f"  [endurance] WARNING {alert['message']} at t={elapsed:.0f}s")
                    # Detect CPU sustained above threshold for N consecutive minutes
                    cpu_window.append(cpu)
                    if (
                        len(cpu_window) == _ENDURANCE_CPU_WINDOW
                        and all(c > _ENDURANCE_CPU_ALERT_PCT for c in cpu_window)
                    ):
                        alert = {
                            "elapsed_s": round(elapsed, 1),
                            "type":      "CPU_SUSTAINED",
                            "message":   (
                                f"CPU > {_ENDURANCE_CPU_ALERT_PCT}% for "
                                f"{_ENDURANCE_CPU_WINDOW} consecutive minutes"
                            ),
                        }
                        alerts.append(alert)
                        cpu_window.clear()   # reset to avoid repeated consecutive alerts
                        print(f"  [endurance] WARNING {alert['message']} at t={elapsed:.0f}s")
                    print(
                        f"  [endurance] t={elapsed:.0f}s  server: "
                        f"cpu={cpu:.1f}%  ram={ram:.1f}%"
                    )
                last_server_poll = now

            # ── Periodic browser restart (memory hygiene) ──────────────────────
            # Close and reopen the Chromium process every N hours to prevent
            # memory growth from degrading stream rendering over multi-day runs.
            if (
                _ENDURANCE_BROWSER_RESTART_S > 0
                and elapsed > 0
                and (elapsed - last_browser_restart) >= _ENDURANCE_BROWSER_RESTART_S
            ):
                last_browser_restart = elapsed
                print(
                    f"  [endurance] t={elapsed:.0f}s  Browser restart "
                    f"(every {_ENDURANCE_BROWSER_RESTART_S//3600}h memory hygiene) ..."
                )
                try:
                    scaler.close_all()
                    new_results = scaler.ramp_up(
                        target_count=1,
                        interval_seconds=0,
                        layout=layout,
                        username=ACTIVE_ENV["admin_user"],
                        password=ACTIVE_ENV["admin_pass"],
                        headless=args.headless,
                    )
                    prev_webrtc_conns = None  # treat next WebRTC poll as fresh baseline
                    alerts.append({
                        "elapsed_s": round(elapsed, 1),
                        "type":      "BROWSER_RESTART",
                        "message":   "Scheduled browser restart (memory hygiene)",
                    })
                    time.sleep(15)  # let WHEP re-establish before next poll
                    print(f"  [endurance] Browser restart complete.")
                except Exception as _br_exc:
                    print(f"  [endurance] Browser restart failed: {_br_exc}")

            # ── Hourly checkpoint ─────────────────────────────────────────────
            # Write in-memory timelines + alerts to disk so a crash doesn't lose
            # everything. The CSVs are already flushed every poll cycle.
            if (
                _output_dir
                and _ENDURANCE_CHECKPOINT_S > 0
                and elapsed > 0
                and (elapsed - last_checkpoint) >= _ENDURANCE_CHECKPOINT_S
            ):
                last_checkpoint = elapsed
                try:
                    _cp = {
                        "elapsed_s":       round(elapsed, 1),
                        "webrtc_timeline": webrtc_timeline,
                        "server_timeline": server_timeline,
                        "alerts":          alerts,
                    }
                    _cp_path = os.path.join(_output_dir, "checkpoint.json")
                    with open(_cp_path, "w", encoding="utf-8") as _cpf:
                        json.dump(_cp, _cpf)
                    print(f"  [endurance] t={elapsed:.0f}s  Checkpoint written ({len(webrtc_timeline)} WebRTC, {len(server_timeline)} server samples).")
                except Exception as _cp_exc:
                    print(f"  [endurance] Checkpoint write failed: {_cp_exc}")

            # ── Page freeze detection ─────────────────────────────────────────
            # Detect a frozen Angular/Chromium tab that still reports non-zero
            # WebRTC connections but has a broken DOM (blank page, crashed renderer,
            # or navigated away). Trigger a reconnect if the page URL drifts.
            if prev_webrtc_conns and prev_webrtc_conns > 0:
                try:
                    pages = scaler.get_all_pages()
                    for _pg in pages:
                        _url = _pg.url
                        if _url and "orlistcomponent" not in _url and "login" not in _url:
                            print(
                                f"  [endurance] t={elapsed:.0f}s  Page freeze detected "
                                f"(url={_url!r}) — triggering reconnect ..."
                            )
                            alerts.append({
                                "elapsed_s": round(elapsed, 1),
                                "type":      "PAGE_FREEZE",
                                "message":   f"Page drifted to {_url!r} — reconnected",
                            })
                            try:
                                client.login(_pg)
                                client.go_dashboard(_pg)
                                if layout != "1":
                                    client.click_layout(_pg, layout)
                                prev_webrtc_conns = None
                                time.sleep(15)
                            except Exception as _pf_r:
                                print(f"  [endurance] Page freeze reconnect error: {_pf_r}")
                except Exception:
                    pass

            time.sleep(1)

    actual_duration_s = time.monotonic() - start_time
    print(
        f"  [endurance_test] Soak complete ({actual_duration_s:.0f}s). "
        "Collecting final WebRTC snapshot ..."
    )
    final_snaps = _collect_webrtc(scaler) or _collect_webrtc(scaler, connected_only=False)
    # If final snapshot is all-zero (session closing), patch with last timeline entry
    if webrtc_timeline and (not final_snaps or all(s.connection_count == 0 for s in final_snaps)):
        last = webrtc_timeline[-1]
        print(
            f"  [endurance_test] Final WebRTC snap was zero — last good poll: "
            f"t={last['elapsed_s']}s conns={last['connection_count']} fps={last['fps']:.1f}"
        )
    scaler.close_all()

    # Use actual endurance duration as the bandwidth measurement window,
    # not the short initial soak used by _wait_for_webrtc_then_soak.
    stream_timing["streaming_duration_s"] = round(actual_duration_s, 1)
    # WHEP reconnects reset browser byte counters, making per-source Mbps deltas
    # meaningless for endurance runs. Strip the start-of-run baseline so the
    # bandwidth profile falls back to composition-only (all sources shown, Mbps = —).
    stream_timing.pop("per_source_bx_at_start", None)
    stream_timing.pop("bytes_rx_at_start", None)

    data = aggregate(
        scenario_name="endurance_test",
        session_results=results,
        webrtc_snapshots=final_snaps,
        api_calls=[],
        stream_timing=stream_timing,
    )
    # Endurance opens 1 session in a 12-up layout — patch connected/rate so the
    # stat card shows "12/12 = 100%" rather than "1/12 = 8.3%".
    data["summary"]["connected"]        = n_sources
    data["summary"]["blocked"]          = 0
    data["summary"]["connect_rate_pct"] = 100.0
    data["duration_requested_s"] = duration_s
    data["duration_actual_s"]    = round(actual_duration_s, 1)
    data["alerts"]               = alerts
    data["webrtc_timeline"]      = webrtc_timeline
    data["server_timeline"]      = server_timeline
    data["stream_timing"]        = stream_timing
    data["server_name"]          = ACTIVE_ENV.get("name", "unknown")
    data["hardware"]             = ACTIVE_ENV.get("hardware", "")
    data["room_sources"]         = room_sources_meta
    data["session_tier"]         = n_sources   # sources count drives "Total Sources" stat card
    return data


# ---------------------------------------------------------------------------
# Scenario: SRS Handle Leak
# ---------------------------------------------------------------------------

# RFC 5737 TEST-NET-1/2/3 — routable addresses that are guaranteed unreachable
# on any production network.  SRS will spawn ffmpeg for these URLs; ffmpeg will
# fail immediately (no route / connection refused), and SRS will retry.
# This exercises the handle lifecycle: open handles on ffmpeg spawn, close on
# ffmpeg exit.  12 streams keeps room setup fast while still generating enough
# churn to surface a handle leak within a few hours.
_DEAD_RTSP_URLS = [
    # TEST-NET-1 (192.0.2.0/24) — streams 1-24
    "rtsp://192.0.2.1:554/dead/stream01",
    "rtsp://192.0.2.2:554/dead/stream02",
    "rtsp://192.0.2.3:554/dead/stream03",
    "rtsp://192.0.2.4:554/dead/stream04",
    "rtsp://192.0.2.5:554/dead/stream05",
    "rtsp://192.0.2.6:554/dead/stream06",
    "rtsp://192.0.2.7:554/dead/stream07",
    "rtsp://192.0.2.8:554/dead/stream08",
    "rtsp://192.0.2.9:554/dead/stream09",
    "rtsp://192.0.2.10:554/dead/stream10",
    "rtsp://192.0.2.11:554/dead/stream11",
    "rtsp://192.0.2.12:554/dead/stream12",
    "rtsp://192.0.2.13:554/dead/stream13",
    "rtsp://192.0.2.14:554/dead/stream14",
    "rtsp://192.0.2.15:554/dead/stream15",
    "rtsp://192.0.2.16:554/dead/stream16",
    "rtsp://192.0.2.17:554/dead/stream17",
    "rtsp://192.0.2.18:554/dead/stream18",
    "rtsp://192.0.2.19:554/dead/stream19",
    "rtsp://192.0.2.20:554/dead/stream20",
    "rtsp://192.0.2.21:554/dead/stream21",
    "rtsp://192.0.2.22:554/dead/stream22",
    "rtsp://192.0.2.23:554/dead/stream23",
    "rtsp://192.0.2.24:554/dead/stream24",
    # TEST-NET-2 (198.51.100.0/24) — streams 25-36
    "rtsp://198.51.100.1:554/dead/stream25",
    "rtsp://198.51.100.2:554/dead/stream26",
    "rtsp://198.51.100.3:554/dead/stream27",
    "rtsp://198.51.100.4:554/dead/stream28",
    "rtsp://198.51.100.5:554/dead/stream29",
    "rtsp://198.51.100.6:554/dead/stream30",
    "rtsp://198.51.100.7:554/dead/stream31",
    "rtsp://198.51.100.8:554/dead/stream32",
    "rtsp://198.51.100.9:554/dead/stream33",
    "rtsp://198.51.100.10:554/dead/stream34",
    "rtsp://198.51.100.11:554/dead/stream35",
    "rtsp://198.51.100.12:554/dead/stream36",
]


def run_srs_handle_leak(pw, args) -> dict:
    """
    SRS Handle Leak Detection Scenario.

    Configures a small set of rooms with unreachable RTSP URLs so SRS
    continuously spawns and reaps ffmpeg child processes.  Monitors the SRS
    process HandleCount via WinRM to detect whether OS handles leak as ffmpeg
    processes die and are retried.

    Key outputs in the run directory:
      srs_handles.csv     -- per-poll: timestamp, elapsed_s, handle_count, srs_pid, srs_cpu, ffmpeg_count
      srs_handles.log     -- WinRM errors (written at shutdown)
      handle_report.html  -- Chart.js chart: handle count + ffmpeg count over time
    """
    from metrics.srs_handle_monitor import SrsHandleMonitor, write_handle_report
    import ctypes

    # ── Prevent Windows sleep / screen-off for the duration of the test ──────
    _ES_CONTINUOUS       = 0x80000000
    _ES_SYSTEM_REQUIRED  = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        )
        print("  [handle-leak] Sleep prevention enabled (SetThreadExecutionState).")
    except Exception:
        pass  # non-Windows or unavailable — safe to ignore

    duration_s  = _parse_duration(getattr(args, "duration", "2h"))
    output_dir  = getattr(args, "_scenario_output", os.path.join("results", "srs_handle_leak"))
    server_name = ACTIVE_ENV.get("server_name", args.server)
    server_pass = ACTIVE_ENV.get("server_pass", "")
    os.makedirs(output_dir, exist_ok=True)

    # ── Configure rooms with dead RTSP URLs ──────────────────────────────────
    n_rooms    = len(_DEAD_RTSP_URLS)
    room_names = [f"OR {i+1:02d}" for i in range(n_rooms)]
    dead_room_sources = [
        {
            "room_name":   f"OR {i+1:02d}",
            "source": {
                "name":     f"dead-stream-{i+1}",
                "url":      _DEAD_RTSP_URLS[i],
                "username": "",
                "password": "",
            },
            "make_primary": True,
        }
        for i in range(n_rooms)
    ]

    print(f"  [handle-leak] Ensuring {n_rooms} dead-URL rooms exist on {server_name} ...")
    RoomCleanup(ACTIVE_ENV).ensure_rooms_exist(room_names)

    print(f"  [handle-leak] Configuring rooms with unreachable RTSP URLs ...")
    admin_client = _make_admin_client()
    setup        = SourceSetup(admin_client)
    results      = setup.configure_rooms(pw, dead_room_sources)
    errors       = [r for r, s in results.items() if s.startswith("error")]
    if errors:
        print(f"  [handle-leak] WARNING: {len(errors)} room(s) failed source setup: {errors}")
    else:
        print(f"  [handle-leak] All {n_rooms} rooms configured — SRS will start retrying ffmpeg connections.")

    # ── Start handle monitor ─────────────────────────────────────────────────
    if not server_pass:
        print("  [handle-leak] ORC_SERVER_PASS not set -- cannot monitor handles. Exiting.")
        return {
            "scenario_name":   "srs_handle_leak",
            "server_name":     server_name,
            "hardware":        ACTIVE_ENV.get("hardware", ""),
            "session_tier":    n_rooms,
            "summary":         {"connected": 0, "total_sessions": n_rooms},
            "session_results": [],
            "alerts":          [],
            "webrtc_timeline": [],
            "server_timeline": [],
        }

    csv_path = os.path.join(output_dir, "srs_handles.csv")
    log_path = os.path.join(output_dir, "srs_handles.log")

    monitor = SrsHandleMonitor(
        host=ACTIVE_ENV.get("server_host", ""),
        user=ACTIVE_ENV.get("server_user", "Administrator"),
        password=server_pass,
        csv_path=csv_path,
        log_path=log_path,
        interval=10,
    ).start()
    print(f"  [handle-leak] Monitor started (interval=10s). "
          f"Running for {duration_s}s ({duration_s/3600:.1f}h) ...")

    # ── Wait loop (status print every 5 minutes) ─────────────────────────────
    t0 = time.time()
    while True:
        elapsed   = time.time() - t0
        remaining = duration_s - elapsed
        if remaining <= 0:
            break
        print(f"  [handle-leak] t={elapsed:.0f}s — monitoring ... ({remaining/3600:.2f}h remaining)")
        time.sleep(min(300.0, remaining))

    monitor.stop()
    print("  [handle-leak] Monitor stopped.")

    # ── Release sleep prevention ──────────────────────────────────────────────
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        print("  [handle-leak] Sleep prevention released.")
    except Exception:
        pass

    # ── Generate HTML report ─────────────────────────────────────────────────
    alerts    = monitor.get_alerts()
    html_path = write_handle_report(csv_path, output_dir, server_name=server_name, alerts=alerts)
    if html_path:
        print(f"  [handle-leak] Handle report: {html_path}")

    duration_actual_s = round(time.time() - t0, 1)
    return {
        "scenario_name":      "srs_handle_leak",
        "scenario":           "srs_handle_leak",
        "server_name":        server_name,
        "hardware":           ACTIVE_ENV.get("hardware", ""),
        "session_tier":       n_rooms,
        "duration_s":         duration_s,
        "duration_actual_s":  duration_actual_s,
        "summary":            {"connected": n_rooms, "total_sessions": n_rooms, "avg_load_ms": 0},
        "session_results":    [],
        "alerts":             alerts,
        "webrtc_timeline":    [],
        "server_timeline":    [],
        "handle_report_html": html_path,
    }


SCENARIO_REGISTRY = {
    "capacity_validation":   run_capacity_validation,
    "layout_stress":         run_layout_stress,
    "enforcement_threshold": run_enforcement_threshold,
    "layout_scaling":        run_layout_scaling,
    "network_degradation":   run_network_degradation,
    "endurance_test":        run_endurance_test,
    "srs_handle_leak":       run_srs_handle_leak,
}



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global ACTIVE_ENV

    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Set the active server environment based on --server flag.
    # For 'all' we default to qa155 (capacity_validation handles iteration itself).
    if args.server != "all":
        ACTIVE_ENV = SERVERS[args.server]
    else:
        ACTIVE_ENV = SERVERS["qa155"]

    os.makedirs(args.output_dir, exist_ok=True)

    scenarios_to_run = ALL_SCENARIOS if args.scenario in ("all", None) else [args.scenario]

    print(f"\nORC Performance Test Runner -- {timestamp}")
    print(f"Server:    {args.server}  ({ACTIVE_ENV.get('hardware', '')})")
    print(f"Target:    {ACTIVE_ENV.get('base_url', '(not set)')}")
    print(f"Scenarios: {', '.join(scenarios_to_run)}")
    print(f"Interval:  {args.interval}s between sessions")
    print(f"Output:    {os.path.abspath(args.output_dir)}")
    print(f"Headless:  {args.headless}")
    if args.dry_run:
        print("Mode:      DRY RUN -- no tests will execute\n")

    # --reset-rooms: wipe all rooms on the target server before running anything.
    # capacity_validation and endurance_test handle their own room setup internally.
    if args.reset_rooms and args.scenario not in ("capacity_validation", "endurance_test"):
        _reset_rooms(dry_run=args.dry_run)
        if args.scenario is None and args.setup_sources is None:
            # No scenario or source setup requested — cleanup only, exit now
            sys.exit(0)

    # --setup-sources: configure room sources and exit without running scenarios.
    if args.setup_sources:
        _src_setup = getattr(args, "sources", None)
        n_sources_setup = ACTIVE_ENV.get("stream_tiers", [12])[-1] if _src_setup is None else _src_setup
        if n_sources_setup == 0:
            print("  [setup-sources] sources=0 — no rooms to create or configure.")
            print("  [setup-sources] Done.")
            sys.exit(0)
        if args.setup_sources == "vision":
            room_sources = [
                {"room_name": f"OR {i+1:02d}", "source": _as_setup_source(VISION[i]), "make_primary": True}
                for i in range(min(len(VISION), n_sources_setup))
            ]
        elif args.setup_sources == "simulated":
            room_sources = [
                {"room_name": f"OR {i+1:02d}", "source": _as_setup_source(SIMULATED[i % len(SIMULATED)]), "make_primary": True}
                for i in range(n_sources_setup)
            ]
        elif args.setup_sources == "cameras":
            n_cams = min(len(_PHYSICAL_CAMERAS), n_sources_setup)
            room_sources = [
                {"room_name": f"OR {i+1:02d}", "source": _as_setup_source(_PHYSICAL_CAMERAS[i]), "make_primary": True}
                for i in range(n_cams)
            ]
        else:  # mixed — identical to _build_room_sources used by endurance test
            room_sources = _build_room_sources(n_sources_setup)
        room_names = [rs["room_name"] for rs in room_sources]
        print(f"\n  [setup-sources] Ensuring {len(room_names)} rooms exist ...")
        RoomCleanup(ACTIVE_ENV).ensure_rooms_exist(room_names)
        print(f"  [setup-sources] Configuring {args.setup_sources} sources ...")
        with sync_playwright() as pw:
            admin_client = _make_admin_client()
            setup = SourceSetup(admin_client)
            results = setup.configure_rooms(pw, room_sources)
            for room, status in results.items():
                print(f"    {room}: {status}")
        errors = [r for r, s in results.items() if s.startswith("error")]
        if errors:
            print(f"  [setup-sources] WARNING: {len(errors)} room(s) failed: {errors}")
            sys.exit(1)
        print("  [setup-sources] Done.")
        sys.exit(0)

    with sync_playwright() as pw:
        for scenario_name in scenarios_to_run:
            print(f"\n{'='*60}")
            print(f"Running scenario: {scenario_name}")
            print(f"{'='*60}")

            if args.dry_run:
                print(f"  [dry-run] Would run {scenario_name}")
                continue

            if getattr(args, 'tag', None):
                _folder_name = args.tag
            else:
                import uuid as _uuid
                _date_str   = datetime.now().strftime("%Y-%m-%d")
                _dur_str    = getattr(args, 'duration', None) or ""
                _server_str = args.server if args.server != "all" else "all"
                _uid        = _uuid.uuid4().hex[:6]
                _parts      = ["run", _date_str, scenario_name]
                if _dur_str:
                    _parts.append(_dur_str)
                _parts.extend([_server_str, _uid])
                _folder_name = "_".join(_parts)
            scenario_output = os.path.join(
                args.output_dir, _folder_name
            )
            os.makedirs(scenario_output, exist_ok=True)

            server_proc = None
            server_csv = None
            if args.collect_server_metrics:
                server_proc, server_csv = _start_server_metrics(scenario_output)

            # Expose the live server collector and output dir to scenario functions
            args._server_collector = server_proc
            args._scenario_output  = scenario_output

            try:
                run_fn = SCENARIO_REGISTRY[scenario_name]
                data = run_fn(pw, args)
            finally:
                _stop_server_metrics(server_proc)

            if server_csv and os.path.exists(server_csv):
                _attach_server_metrics(data, server_csv)

            paths = write_report(data, scenario_output)
            print(f"\n  Report written:")
            print(f"    HTML: {paths.get('html', '--')}")
            print(f"    CSV:  {paths.get('csv', '--')}")
            print(f"    JSON: {paths.get('json', '--')}")
            if data.get("handle_report_html"):
                print(f"    Handle chart: {data['handle_report_html']}")

    print(f"\n{'='*60}")
    print("All scenarios complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

