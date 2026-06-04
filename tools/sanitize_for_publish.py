"""
tools/sanitize_for_publish.py

Sanitizes HTML report files for public/shared hosting by removing:
  - Credentials embedded in RTSP/RTSPS stream URLs  (user:pass@...)
  - Internal network IP addresses                    (10.101.x.x  → [INTERNAL])
  - Internal Active Directory domain suffix          (.actdev.local)

Writes sanitized copies to docs/reports/<run-name>/report.html.
Does NOT modify the original files under results/.

Usage:
    python tools/sanitize_for_publish.py
"""

import os
import re
import shutil

# ---------------------------------------------------------------------------
# Source → destination mapping
# ---------------------------------------------------------------------------
REPORT_JOBS = [
    {
        "src": "results/endurance_test_20260528_101136/report.html",
        "dst": "docs/reports/endurance-qa162-10min/report.html",
    },
    {
        "src": "results/endurance_test_qa162_4d_20260529_134635/report.html",
        "dst": "docs/reports/endurance-qa162-4day/report.html",
    },
]

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


def sanitize(content: str) -> str:
    for pattern, replacement in RULES:
        content = pattern.sub(replacement, content)
    return content


def process(src: str, dst: str) -> None:
    print(f"  Reading  : {src}")
    with open(src, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    original_len = len(content)
    content = sanitize(content)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(content)

    print(f"  Written  : {dst}  ({original_len:,} → {len(content):,} chars)")


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
        if not os.path.exists(src):
            print(f"  SKIP (not found): {src}")
            continue
        process(src, dst)
        verify(dst)
        print()

    print("Done.")
