"""
tools/sanitize_for_publish.py

Sanitizes HTML report files for public/shared hosting by removing:
  - Credentials embedded in RTSP/RTSPS stream URLs  (user:pass@...)
  - Internal network IP addresses                    (10.101.x.x  → [INTERNAL])
  - Internal Active Directory domain suffix          (.actdev.local)

Also downsamples time-series arrays inside window._reportData to reduce file
size for reports with many hours of polling data (e.g. 4-day endurance tests).
The DOWNSAMPLE_FACTOR controls how aggressively to thin the data — a factor of
10 keeps every 10th sample, reducing a 38,000-point series to 3,800 points
with no visible difference on charts.

Writes sanitized copies to docs/reports/<run-name>/report.html.
Does NOT modify the original files under results/.

Usage:
    python tools/sanitize_for_publish.py
"""

import json
import os
import re

# ---------------------------------------------------------------------------
# Source → destination mapping
# ---------------------------------------------------------------------------
REPORT_JOBS = [
    {
        "src": "results/endurance_test_qa162_4d_20260529_134635/report.html",
        "dst": "docs/reports/endurance-qa162-4day/report.html",
        "downsample": 10,  # 4 days × 9s polling → keep every 10th point
    },
]

# Time-series keys inside window._reportData whose arrays should be downsampled.
# Use dot-notation for nested keys (e.g. "server.rows" = data["server"]["rows"]).
# Each value is a list of objects; we thin them by keeping every Nth element.
_TIMELINE_KEYS = [
    "server_timeline",
    "webrtc_timeline",
    "server.rows",
    "server.proc_series",
    "server.process_spikes",  # 248k items in 4-day run — biggest contributor
]


def _get_nested(d: dict, dotkey: str):
    keys = dotkey.split(".")
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None, None
        parent, d = d, d[k]
    return parent, keys[-1]


def _set_nested(d: dict, dotkey: str, value) -> None:
    keys = dotkey.split(".")
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = value

# ---------------------------------------------------------------------------
# Sanitization rules (applied in order)
# ---------------------------------------------------------------------------
RULES = [
    # 1. Strip credentials from RTSP/RTSPS URLs: proto://user:pass@host → proto://host
    #    Handles both rtsps:// and rtsp://
    (re.compile(r'(rtsps?://)[^:@/\s"\'<>]+:[^@\s"\'<>]+@'), r'\1'),

    # 2. Mask internal 10.101.x.x IP addresses everywhere
    (re.compile(r'\b10\.101\.\d{1,3}\.\d{1,3}\b'), '[INTERNAL]'),

    # 3. Strip internal Active Directory domain suffix
    (re.compile(r'\.actdev\.local\b', re.IGNORECASE), ''),
]


def _downsample_report_data(content: str, factor: int) -> tuple[str, str]:
    """
    Locate window._reportData = {...}; in the HTML, parse the JSON, thin all
    time-series arrays by keeping every `factor`-th element, then substitute
    the compacted JSON back in.  Returns (new_content, summary_string).
    """
    pattern = re.compile(
        r'(window\._reportData\s*=\s*)(\{.*?\})(\s*;)',
        re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        return content, "  (no _reportData blob found — skipping downsample)"

    prefix, json_blob, suffix = m.group(1), m.group(2), m.group(3)
    before_kb = len(json_blob) // 1024

    try:
        data = json.loads(json_blob)
    except json.JSONDecodeError as exc:
        return content, f"  ⚠  JSON parse failed: {exc} — skipping downsample"

    thinned = {}
    for key in _TIMELINE_KEYS:
        parent, leaf = _get_nested(data, key)
        if parent is None or not isinstance(parent[leaf], list):
            continue
        original = parent[leaf]
        parent[leaf] = original[::factor]
        thinned[key] = (len(original), len(parent[leaf]))

    compact = json.dumps(data, separators=(",", ":"))
    after_kb = len(compact) // 1024
    new_content = content[:m.start()] + prefix + compact + suffix + content[m.end():]

    summary_lines = [f"  Downsampled _reportData: {before_kb:,}KB → {after_kb:,}KB (factor {factor}×)"]
    for key, (before, after) in thinned.items():
        summary_lines.append(f"    {key}: {before:,} → {after:,} points")
    return new_content, "\n".join(summary_lines)


def sanitize(content: str) -> str:
    for pattern, replacement in RULES:
        content = pattern.sub(replacement, content)
    return content


def process(src: str, dst: str, downsample: int = 1) -> None:
    print(f"  Reading  : {src}")
    with open(src, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    original_len = len(content)

    if downsample > 1:
        print(f"  Downsampling time-series (factor {downsample}×) ...")
        content, ds_summary = _downsample_report_data(content, downsample)
        print(ds_summary)

    content = sanitize(content)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(content)

    final_mb = len(content) / 1024 / 1024
    print(f"  Written  : {dst}  ({original_len//1024//1024}MB → {final_mb:.1f}MB)")


def verify(dst: str) -> None:
    """Confirm no credentials or internal IPs remain."""
    with open(dst, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    issues = []
    if re.search(r'rtsps?://[^:@/\s"\'<>]+:[^@\s"\'<>]+@', content):
        issues.append("RTSP credentials still present")
    if re.search(r'\b10\.101\.\d{1,3}\.\d{1,3}\b', content):
        issues.append("Internal IP (10.101.x.x) still present")
    if re.search(r'\.actdev\.local\b', content, re.IGNORECASE):
        issues.append(".actdev.local domain still present")
    # Check for common password strings from .env (belt-and-suspenders)
    for secret in ("Arthrex123", "leSTReNSu99", "MTv8DUYl"):
        if secret in content:
            issues.append(f"Possible credential string '{secret}' still present")

    if issues:
        print(f"  ⚠  VERIFY FAILED for {dst}:")
        for i in issues:
            print(f"     - {i}")
    else:
        print(f"  ✓  Clean  : {dst}")


if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(base)

    print("ORC Report Sanitizer")
    print("=" * 50)
    for job in REPORT_JOBS:
        src, dst = job["src"], job["dst"]
        downsample = job.get("downsample", 1)
        if not os.path.exists(src):
            print(f"  SKIP (not found): {src}")
            continue
        process(src, dst, downsample=downsample)
        verify(dst)
        print()

    print("Done.")
