# ORC Performance Test Suite

**📊 Reports & Capacity Calculator:** https://daniel-rodriguez-arthrex.github.io/orc-performance-reports/

Automated performance testing for **ORC (OR Command) 2.1.0**, targeting session scaling, bandwidth enforcement, WebRTC stream stability, layout responsiveness, and long-duration soak testing using Playwright and WinRM server metrics.

---

## Overview

This project exercises the ORC application under realistic load conditions:

- Ramps concurrent OR-room sessions and measures WebRTC delivery, CPU/RAM, and API latency
- Verifies bandwidth enforcement caps block sessions when exceeded
- Benchmarks API latency during layout changes (1 → 4 → 9 → 12 video feeds)
- Runs multi-hour soak tests to detect RAM drift, WebRTC drops, and CPU anomalies
- Captures server-side CPU/memory/network via WinRM Python polling
- Optionally injects packet loss and latency via [Clumsy](https://jagt.github.io/clumsy/)

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10+ |
| Playwright (Python) | 1.44+ |
| WinRM access | ORC server host (server metrics only) |
| Clumsy | 0.3+ (network degradation scenario only) |

WinRM must be enabled on the ORC server host. Run on the server as Administrator:

```powershell
Enable-PSRemoting -Force
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "<your-machine-IP>"
```

---

## Setup

### 1. Copy and fill in environment variables

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `BASE_URL` | ORC application base URL (e.g. `https://orc-qa-155.actdev.local`) |
| `SERVER_HOST` | Hostname or IP of the ORC server (for WinRM) |
| `ADMIN_USER` | ORC admin username |
| `ADMIN_PASS` | ORC admin password |
| `NON_ADMIN_USER` | ORC non-admin (MatrixUser) username |
| `NON_ADMIN_PASS` | ORC non-admin password |
| `SERVER_USER` | Windows account for WinRM connection |
| `SERVER_PASS` | Windows password for WinRM connection |

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browser

```bash
playwright install chromium
```

---

## Configuration

### Stream Sources (`config/sources.py`)

| List | Description | Count | Bitrate |
|---|---|---|---|
| `SIMULATED` | Matrix RTSP streams (`rtsp://10.101.64.68:8554/orc/stream1–20`) | 20 | ~10 Mbps each |
| `VISION` | Vision RTSPS streams | 6 | ~12 Mbps each |
| `CAMERAS` | Physical Sony/Axis cameras | varies | varies |

### Headless Mode

| Flag | WebRTC | Use when |
|---|---|---|
| *(omit)* | Full | Debugging, watching sessions open live |
| `--headless` | Full | **Standard for all test runs** — Chrome new headless, full GPU + WebRTC pipeline |

---

## Scenario Quick Reference

| Scenario | Description | Key flags |
|---|---|---|
| `capacity_validation` | Multi-tier ramp test — opens N sessions, collects WebRTC + CPU/RAM | `--tiers`, `--layout`, `--server` |
| `endurance_test` | Single session, runs for hours, detects RAM drift / WebRTC drops | `--duration`, `--layout` |
| `enforcement_threshold` | Verify bandwidth cap blocks sessions at the correct count | — |
| `layout_scaling` | Single session cycles 1→4→9→12 layouts, captures API latency | — |
| `layout_stress` | All sessions cycle layouts concurrently, measures WebRTC at each step | `--tiers`, `--layout` |
| `network_degradation` | 3 sessions under Clumsy presets (light/moderate/heavy/severe) | — |

---

## Running Tests

### Capacity Validation (primary load test)

**Step 1 — Configure room sources** (one-time, or when rooms change):

```bash
# Vision RTSPS streams (OR 01-06)
python run_perf.py --reset-rooms --setup-sources vision

# Simulated Matrix RTSP streams (OR 01-12)
python run_perf.py --reset-rooms --setup-sources simulated

# Mixed: Vision OR 01-04 + Simulated OR 05-08
python run_perf.py --reset-rooms --setup-sources mixed
```

**Step 2 — Run capacity validation:**

```bash
# Standard 12-session test, Chrome new headless (full WebRTC), server metrics
python run_perf.py --scenario capacity_validation --tiers 12 \
    --headless=new --collect-server-metrics

# Custom session count and layout
python run_perf.py --scenario capacity_validation --tiers 8 --layout 9 \
    --headless=new --collect-server-metrics

# Multiple tiers in sequence (12 → 24 → 36)
python run_perf.py --scenario capacity_validation --tiers 12 24 36 \
    --headless=new --collect-server-metrics

# Slower ramp-up (60s between sessions instead of 30s)
python run_perf.py --scenario capacity_validation --tiers 12 --interval 60
```

**Multi-server comparison:**

```bash
# Run capacity validation against all configured servers sequentially
python run_perf.py --scenario capacity_validation --server all \
    --headless=new --collect-server-metrics
```

### Endurance / Soak Test

Runs a single session for hours, polling WebRTC every 5 minutes and server metrics every 60 seconds. Detects connection drops, RAM drift >20%, and CPU sustained above 70% for 10+ minutes.

```bash
# 8-hour soak with server metrics (recommended for overnight runs)
python run_perf.py --scenario endurance_test --duration 8h \
    --headless=new --collect-server-metrics

# 30-minute quick soak
python run_perf.py --scenario endurance_test --duration 30m --headless=new

# Single-stream layout (minimum WebRTC load)
python run_perf.py --scenario endurance_test --duration 4h --layout 1 \
    --headless=new --collect-server-metrics

# Full 12-stream layout (maximum WebRTC load)
python run_perf.py --scenario endurance_test --duration 4h --layout 12 \
    --headless=new --collect-server-metrics
```

Duration format: `8h`, `30m`, `2h30m`, `90m`, `3600s` (plain integer treated as hours).

### Other Scenarios

```bash
# Bandwidth enforcement (Vision sources required — run --setup-sources vision first)
python run_perf.py --scenario enforcement_threshold --headless=new

# Layout API latency (single session)
python run_perf.py --scenario layout_scaling --headless=new

# Concurrent layout cycling stress test
python run_perf.py --scenario layout_stress --tiers 12 --headless=new

# Network degradation (requires Clumsy, run as Administrator)
python run_perf.py --scenario network_degradation --headless
```

### Common flags

```bash
# Custom output directory
python run_perf.py --scenario capacity_validation --tiers 12 --output-dir results/sprint-42

# Preview what would run without executing
python run_perf.py --scenario capacity_validation --dry-run

# Reset rooms before a test
python run_perf.py --reset-rooms --scenario capacity_validation --tiers 12
```

---

## Migration Guide (retired scenarios)

Three scenarios were consolidated into `capacity_validation` with configurable flags. Use `--setup-sources` to configure room sources first, then run `capacity_validation`:

| Old command | New equivalent |
|---|---|
| `--scenario matrix_baseline` | `--setup-sources simulated` → `--scenario capacity_validation --tiers 8` |
| `--scenario vision_baseline` | `--setup-sources vision` → `--scenario capacity_validation --tiers 7` |
| `--scenario mixed_baseline` | `--setup-sources mixed` → `--scenario capacity_validation --tiers 8` |

---

## Output

Each scenario run creates a timestamped subdirectory under `--output-dir`:

```
results/
└── cap_val_orc-qa-155_12sessions/
    ├── report.html          # Interactive HTML report with charts and verdict banner
    ├── results.csv          # Flat row-per-session metrics table
    ├── results.json         # Full structured data for downstream analysis
    └── server_metrics.csv   # WinRM CPU/memory/network samples (if --collect-server-metrics)
```

The HTML report includes:
- PASS / CAUTION / FAIL / ABORTED verdict banner with threshold findings
- Collapsible source configuration summary
- Session load-time table and chart
- Stream delivery timeline (WebRTC connection count vs. elapsed seconds)
- API latency breakdown
- WebRTC stats per session (FPS, jitter, bytes received)
- Server CPU / RAM / network chart
- Tier comparison table (multi-tier runs)

---

## Server Metrics

When `--collect-server-metrics` is passed, a background thread polls the ORC server host via WinRM NTLM every 5 seconds and writes rows to `server_metrics.csv`.

Requirements:
- `SERVER_HOST`, `SERVER_USER`, `SERVER_PASS` must be set in `.env`
- WinRM must be enabled on the server (see Prerequisites)
- Test machine must be on the same network as the ORC server

For `capacity_validation`, live CPU/RAM readings are also used to **automatically abort the ramp-up** if the server reaches the fail threshold (default: CPU ≥ 85% or RAM ≥ 85%). Rooms are reset after a clean abort.

---

## Network Degradation (Optional)

1. Download and install [Clumsy 0.3+](https://jagt.github.io/clumsy/).
2. Clumsy **requires Administrator privileges**.
3. Run from an elevated PowerShell prompt:

```powershell
python run_perf.py --scenario network_degradation --headless
```

Preset definitions (configurable in `network/clumsy_control.py`):

| Preset | Packet Loss | Extra Latency |
|---|---|---|
| light | 0.5% | 10 ms |
| moderate | 2% | 50 ms |
| heavy | 5% | 100 ms |
| severe | 10% | 250 ms |

---

## Architecture

`run_perf.py` is the single entry point. It parses args, iterates over selected scenarios, and calls each scenario function with a shared Playwright instance. Each scenario uses `OrcClient` to configure the ORC server (egress cap, login), `SessionScaler` to open and ramp browser sessions, `WebRTCCollector` to pull in-page WebRTC stats, and `ApiLatencyMonitor` to intercept network requests. Raw results are passed to `aggregate()` which normalises them into a unified dict, then `write_report()` serialises that dict to HTML, CSV, and JSON. Server-side metrics are collected by `ServerMetricsCollector` (Python WinRM, background thread) and merged into the aggregate before reporting.

---

## Known Limitations

- **Angular input fields**: Playwright's `fill()` does not trigger Angular's change detection on ORC input elements. All text inputs use `press_sequentially()` instead.
- **`--headless=new` session limit**: Chrome new headless with full GPU pipeline has practical limits on the test client machine (~5–7 concurrent sessions on typical hardware). For ceiling/breaking-point tests beyond that, use `--headless` (old headless, no WebRTC data but less client overhead).
- **Clumsy requires Administrator**: The `network_degradation` scenario will skip silently if Clumsy is not installed or not run with elevated privileges.
- **WinRM firewall**: If `--collect-server-metrics` hangs at startup, verify WinRM port 5985 is open and the trusted hosts list includes your machine.
- **Session timing variance**: The `--interval` default of 30 seconds was tuned for the QA lab. Slower environments may require a longer interval to avoid false bandwidth enforcement triggers during ramp-up.

