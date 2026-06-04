from dataclasses import dataclass, field
from playwright.sync_api import sync_playwright, Page
import time

from metrics.webrtc_collector import WebRTCCollector

_WEBRTC_INSTALLER = WebRTCCollector()


@dataclass
class SessionResult:
    tab_index: int          # 0-based index of this session
    connected: bool         # True = dashboard loaded without modal
    modal_fired: bool       # True = bandwidth enforcement modal appeared
    load_time_ms: float     # ms from navigate start to dashboard loaded (or modal fired)
    layout: str             # layout used: "1","4","9","12","auto"
    username: str           # which user account


class SessionScaler:
    """
    Opens browser sessions incrementally against the ORC dashboard and records
    whether each session connected successfully or was blocked by bandwidth enforcement.

    Designed for performance testing: opens sessions one at a time with a configurable
    ramp-up interval, collects timing metrics, and keeps all sessions open until close_all().

    Usage:
        with sync_playwright() as pw:
            scaler = SessionScaler(orc_client, pw)
            results = scaler.ramp_up(target_count=8, interval_seconds=30, layout="1")
            pages = scaler.get_pages()
            # ... collect WebRTC stats from pages ...
            scaler.close_all()
    """

    def __init__(self, orc_client, pw):
        """
        orc_client: OrcClient instance
        pw: active sync_playwright() context manager result
        """
        self._client = orc_client
        self._pw = pw
        # Each entry is (browser, context, page)
        self._sessions: list[tuple] = []
        self._results: list[SessionResult] = []

    def ramp_up(
        self,
        target_count: int,
        interval_seconds: float = 30.0,
        layout: str = "1",
        username: str = None,
        password: str = None,
        headless: bool = False,
        page_callbacks: list = None,
        abort_check=None,
    ) -> list[SessionResult]:
        """
        Opens `target_count` browser sessions in sequence, each separated by
        `interval_seconds` seconds.

        abort_check : callable() -> str | None
            Called after each session connects (before the interval sleep).
            Return a non-empty string reason to stop adding sessions early;
            return None to continue.  ramp_up sets self.abort_reason on early exit.
        """
        results: list[SessionResult] = []
        effective_username = username or "admin"
        self.abort_reason: str | None = None

        for i in range(target_count):
            session_num = i + 1
            browser, context = self._client.new_browser(self._pw, headless=headless)
            page = context.new_page()
            # Install WebRTC interceptor BEFORE any navigation so RTCPeerConnection
            # instances are captured in window.__orcPCs from the moment they're created.
            _WEBRTC_INSTALLER.install(page)
            # Run any additional pre-navigation callbacks (e.g. ApiLatencyMonitor.attach)
            for cb in (page_callbacks or []):
                try:
                    cb(page)
                except Exception:
                    pass
            self._sessions.append((browser, context, page))

            start_time = time.monotonic()

            # Login — waits for URL redirect to /orlistcomponent internally
            self._client.login(page, username=username, password=password)

            modal_fired = False
            connected = False

            # Check modal immediately after login
            if self._client.modal_visible(page):
                elapsed_ms = (time.monotonic() - start_time) * 1000
                modal_fired = True
                self._client.dismiss_modal(page)
                load_time_ms = elapsed_ms
                print(f"[session {session_num}/{target_count}] BLOCKED — modal fired after login ({load_time_ms:.0f}ms)")
                result = SessionResult(
                    tab_index=i,
                    connected=False,
                    modal_fired=True,
                    load_time_ms=load_time_ms,
                    layout=layout,
                    username=effective_username,
                )
                results.append(result)
                self._results.append(result)
                if i < target_count - 1:
                    time.sleep(interval_seconds)
                continue

            # Navigate to dashboard — waits for load state internally
            try:
                self._client.go_dashboard(page)
            except Exception as _nav_err:
                print(f"[session {session_num}/{target_count}] WARNING — dashboard navigation timed out ({type(_nav_err).__name__}); continuing")

            if self._client.modal_visible(page):
                elapsed_ms = (time.monotonic() - start_time) * 1000
                self._client.dismiss_modal(page)
                print(f"[session {session_num}/{target_count}] BLOCKED — modal fired after dashboard load ({elapsed_ms:.0f}ms)")
                result = SessionResult(
                    tab_index=i,
                    connected=False,
                    modal_fired=True,
                    load_time_ms=elapsed_ms,
                    layout=layout,
                    username=effective_username,
                )
                results.append(result)
                self._results.append(result)
                if i < target_count - 1:
                    time.sleep(interval_seconds)
                continue

            # Capture load time here — before layout click — so UI timeouts don't inflate it
            elapsed_ms = (time.monotonic() - start_time) * 1000

            # Apply layout if not single-up
            if layout != "1":
                try:
                    self._client.click_layout(page, layout)
                except Exception as _layout_err:
                    # Layout picker may not render under heavy server load.
                    # Log and continue — the session is still connected and streaming.
                    print(
                        f"[session {session_num}/{target_count}] WARNING — layout click "
                        f"timed out ({type(_layout_err).__name__}); continuing without layout change"
                    )
                time.sleep(1.0)  # brief settle after layout change

                if self._client.modal_visible(page):
                    elapsed_ms = (time.monotonic() - start_time) * 1000
                    self._client.dismiss_modal(page)
                    print(
                        f"[session {session_num}/{target_count}] BLOCKED — modal fired after "
                        f"{layout}-up layout click ({elapsed_ms:.0f}ms)"
                    )
                    result = SessionResult(
                        tab_index=i,
                        connected=False,
                        modal_fired=True,
                        load_time_ms=elapsed_ms,
                        layout=layout,
                        username=effective_username,
                    )
                    results.append(result)
                    self._results.append(result)
                    if i < target_count - 1:
                        time.sleep(interval_seconds)
                    continue

            # Session connected successfully
            connected = True
            print(f"[session {session_num}/{target_count}] Connected — {elapsed_ms:.0f}ms")
            result = SessionResult(
                tab_index=i,
                connected=True,
                modal_fired=False,
                load_time_ms=elapsed_ms,
                layout=layout,
                username=effective_username,
            )
            results.append(result)
            self._results.append(result)

            if i < target_count - 1:
                # Check abort condition BEFORE sleeping so we don't wait a full
                # interval just to discover we should stop.
                if abort_check:
                    reason = abort_check()
                    if reason:
                        print(f"  [ramp-up] Aborting after session {session_num} — {reason}")
                        self.abort_reason = reason
                        break
                time.sleep(interval_seconds)

        return results

    def close_all(self) -> None:
        """Close all open browsers and contexts. Safe to call multiple times."""
        for browser, context, page in self._sessions:
            try:
                browser.close()
            except Exception:
                pass
        self._sessions.clear()

    def get_pages(self) -> list[Page]:
        """Return list of currently open Page objects (connected sessions only)."""
        connected_indices = {r.tab_index for r in self._results if r.connected}
        return [
            page
            for i, (browser, context, page) in enumerate(self._sessions)
            if i in connected_indices
        ]

    def get_all_pages(self) -> list[Page]:
        """Return ALL open Page objects regardless of connection status."""
        return [page for browser, context, page in self._sessions]

    def session_count(self) -> int:
        """Number of sessions opened so far."""
        return len(self._results)
