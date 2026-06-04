"""
clumsy_control.py — Controls Clumsy for network degradation simulation.

Clumsy is OPTIONAL. If clumsy.exe is not found or enabled=False, all
methods are no-ops and print SKIP_MSG.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

SKIP_MSG = "[clumsy] Skipping — clumsy.exe not found or disabled"

_DEFAULT_SEARCH_PATHS = [
    r"C:\Tools\clumsy\clumsy.exe",
    str(Path(__file__).parent / "clumsy.exe"),
    str(Path(__file__).parent.parent / "clumsy" / "clumsy.exe"),
]

# Clumsy WinDivert filter — capture traffic to/from the ORC server.
# Override via ORC_CLUMSY_SERVER_IP env var or pass filter= to ClusmsyController.
import os as _os
_ORC_SERVER_IP = _os.getenv("ORC_CLUMSY_SERVER_IP", "")
_FILTER = (
    f"ip and (ip.DstAddr == {_ORC_SERVER_IP} or ip.SrcAddr == {_ORC_SERVER_IP})"
    if _ORC_SERVER_IP else ""
)


class ClusmsyController:
    """
    Controls Clumsy for network degradation simulation.

    Clumsy is OPTIONAL — all methods are no-ops if clumsy.exe is not found.
    Set enabled=False in constructor to always skip.
    """

    # Preset degradation profiles matching the 2.0.0 performance test
    PRESETS: dict[str, dict] = {
        "light": {
            # 10 MB/s bandwidth, 500 ms lag, 10 % drop, 10 % throttle
            "lag": 500,
            "drop": 10,
            "throttle_chance": 10,
            "throttle_inbound_bw_kbps": 10000,
        },
        "moderate": {
            "lag": 500,
            "drop": 10,
            "throttle_chance": 10,
            "throttle_inbound_bw_kbps": 8000,
        },
        "heavy": {
            "lag": 500,
            "drop": 10,
            "throttle_chance": 10,
            "throttle_inbound_bw_kbps": 6000,
        },
        "severe": {
            "lag": 500,
            "drop": 10,
            "throttle_chance": 10,
            "throttle_inbound_bw_kbps": 4000,
        },
    }

    def __init__(self, clumsy_path: Optional[str] = None, enabled: bool = True) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._available = False

        if not enabled:
            return

        # Auto-detect clumsy.exe
        candidates = ([clumsy_path] if clumsy_path else []) + _DEFAULT_SEARCH_PATHS
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                self._exe = candidate
                self._available = True
                break

    @property
    def available(self) -> bool:
        """True if clumsy.exe was found and enabled=True."""
        return self._available

    def apply_preset(self, preset_name: str) -> bool:
        """
        Apply a named preset. Returns True if applied, False if unavailable.
        Kills any existing clumsy process first.
        """
        if not self._available:
            print(SKIP_MSG)
            return False

        if preset_name not in self.PRESETS:
            raise ValueError(
                f"Unknown preset '{preset_name}'. Valid presets: {list(self.PRESETS)}"
            )

        p = self.PRESETS[preset_name]
        self.stop()
        cmd = self._build_cmd(
            lag_ms=p.get("lag", 0),
            drop_pct=p.get("drop", 0),
            throttle_chance=p.get("throttle_chance", 0),
            bw_kbps=p.get("throttle_inbound_bw_kbps", 0),
        )
        self._launch(cmd)
        return True

    def apply_custom(self, lag_ms: int = 0, drop_pct: int = 0, bw_kbps: int = 0) -> bool:
        """Apply custom degradation parameters."""
        if not self._available:
            print(SKIP_MSG)
            return False

        self.stop()
        cmd = self._build_cmd(lag_ms=lag_ms, drop_pct=drop_pct, bw_kbps=bw_kbps)
        self._launch(cmd)
        return True

    def stop(self) -> None:
        """Kill the clumsy process if running."""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None

        # Also kill any stray clumsy.exe instances by name (best-effort)
        if self._available:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "clumsy.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def _build_cmd(
        self,
        lag_ms: int = 0,
        drop_pct: int = 0,
        throttle_chance: int = 0,
        bw_kbps: int = 0,
    ) -> list[str]:
        """Build the clumsy.exe command line arguments."""
        cmd: list[str] = [self._exe, "--filter", _FILTER]
        if not _FILTER:
            raise RuntimeError(
                "ORC_CLUMSY_SERVER_IP is not set in .env — "
                "cannot build a Clumsy filter without a target IP."
            )

        if lag_ms > 0:
            cmd += ["--lag", "on", "--lag-time", str(lag_ms)]

        if drop_pct > 0:
            cmd += ["--drop", "on", "--drop-chance", str(drop_pct)]

        if throttle_chance > 0 and bw_kbps > 0:
            cmd += [
                "--throttle", "on",
                "--throttle-chance", str(throttle_chance),
                "--throttle-inbound-bandwidth", str(bw_kbps),
            ]

        return cmd

    def _launch(self, cmd: list[str]) -> None:
        """Start clumsy subprocess (requires elevation)."""
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
