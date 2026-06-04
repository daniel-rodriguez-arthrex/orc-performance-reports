"""
orc_client.py — Reusable ORC Playwright client.

Provides OrcClient, a helper class that wraps common ORC UI interactions:
login/logout, navigation, egress-cap configuration, layout selection, and
modal assertion. Designed to be shared across all ORC performance test scripts.

Angular 18 note: use press_sequentially(text, delay=30) for all text inputs —
Playwright's fill() does NOT trigger Angular reactive-form change detection.
"""

from __future__ import annotations


class OrcClient:
    """Reusable Playwright helper for the ORC web application."""

    LAYOUT_LABELS: dict[str, str] = {
        "1": "page size 1",
        "4": "page size 4",
        "9": "page size 9",
        "12": "Facility Title",
        "auto": "AUTO",
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, env: dict) -> None:
        """
        Parameters
        ----------
        env : dict
            Must contain at minimum:
                base_url  – root URL, e.g. "https://orc.example.com"
                username  – default login username
                password  – default login password
        """
        self.base_url: str = env["base_url"].rstrip("/")
        # Support both generic keys ("username"/"password") and the ENV dict
        # keys used by config/environments.py ("admin_user"/"admin_pass").
        self.username: str = (
            env.get("username")
            or env.get("admin_user")
            or ""
        )
        self.password: str = (
            env.get("password")
            or env.get("admin_pass")
            or ""
        )

        # Convenience URL shortcuts
        self.dashboard: str = f"{self.base_url}/orlistcomponent"
        self.settings: str = f"{self.base_url}/orlistcomponent/settings"
        self.fac_settings: str = (
            f"{self.base_url}/orlistcomponent/settings/facilitysettings"
        )
        self.all_rooms: str = (
            f"{self.base_url}/orlistcomponent/settings/room/roommanagement"
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def login(self, page, username: str | None = None, password: str | None = None) -> None:
        """Navigate to base_url and log in.

        If the page is already on /orlistcomponent the login form is skipped.
        Uses press_sequentially to ensure Angular change detection fires.
        """
        uname = username or self.username
        pwd = password or self.password

        page.goto(self.base_url)
        page.wait_for_load_state("networkidle")

        # Skip login if already authenticated
        if "/orlistcomponent" in page.url and "login" not in page.url:
            return

        page.locator("#username_input").click()
        page.locator("#username_input").press_sequentially(uname, delay=30)

        page.locator("#password_input").click()
        page.locator("#password_input").press_sequentially(pwd, delay=30)

        page.get_by_role("button", name="Login", exact=True).click()
        page.wait_for_url("**/orlistcomponent**", timeout=15_000)

    def go_dashboard(self, page) -> None:
        """Navigate to the ORC dashboard and wait until Angular has settled."""
        page.goto(self.dashboard)
        page.wait_for_load_state("load", timeout=60_000)

    def go_settings(self, page) -> None:
        """Navigate to the ORC settings root."""
        page.goto(self.settings)

    def go_all_rooms(self, page) -> None:
        """Navigate to the All Rooms management page."""
        page.goto(self.all_rooms)

    # ------------------------------------------------------------------
    # Facility settings
    # ------------------------------------------------------------------

    def set_egress_cap(self, page, mbps: int) -> None:
        """Set the facility egress bandwidth cap.

        Parameters
        ----------
        mbps : int
            Desired egress cap in Mbps.
        """
        page.goto(self.fac_settings)
        page.wait_for_load_state("networkidle")

        egress_input = page.locator("#bandwidth-egress-input")
        egress_input.click(click_count=3)   # triple-click selects all existing text
        egress_input.fill(str(mbps))

        page.get_by_role("button", name="Done", exact=True).click()
        page.wait_for_timeout(1_000)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def click_layout(self, page, size: str) -> None:
        """Click the layout radio button corresponding to *size*.

        Parameters
        ----------
        size : str
            One of "1", "4", "9", "12", "auto".
        """
        label = self.LAYOUT_LABELS[size]
        radio = page.get_by_role("radio", name=label)
        # Wait for the element to exist in the DOM (attached), then scroll it
        # into the viewport before clicking. In --headless=new mode the full
        # rendering engine may place the layout panel outside the initial
        # viewport, causing wait_for("visible") to time out.
        radio.wait_for(state="attached", timeout=60_000)
        radio.scroll_into_view_if_needed()
        radio.click()

    # ------------------------------------------------------------------
    # Modal helpers
    # ------------------------------------------------------------------

    def modal_visible(self, page) -> bool:
        """Return True if the bandwidth-warning dialog is currently visible."""
        return page.locator('dialog[role="dialog"]').is_visible()

    def dismiss_modal(self, page) -> None:
        """Click OK on the bandwidth-warning dialog and wait briefly."""
        page.get_by_role("button", name="OK").click()
        page.wait_for_timeout(800)

    def assert_modal(self, page, label: str) -> None:
        """Assert that the bandwidth-warning dialog IS visible.

        Parameters
        ----------
        label : str
            Included in the AssertionError message for test identification.

        Raises
        ------
        AssertionError
            If the dialog is not visible.
        """
        if not self.modal_visible(page):
            raise AssertionError(f"[{label}] Expected bandwidth modal to be visible, but it was not.")

    def assert_no_modal(self, page, label: str, wait_ms: int = 2_000) -> None:
        """Assert that the bandwidth-warning dialog is NOT visible.

        Waits *wait_ms* milliseconds first to allow Angular to render any
        potential modal before checking.

        Parameters
        ----------
        label : str
            Included in the AssertionError message for test identification.
        wait_ms : int
            Milliseconds to wait before checking. Default 2000.

        Raises
        ------
        AssertionError
            If the dialog IS visible.
        """
        page.wait_for_timeout(wait_ms)
        if self.modal_visible(page):
            raise AssertionError(f"[{label}] Expected NO bandwidth modal, but one appeared.")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def logout(self, page) -> None:
        """Log out of the ORC application.

        Clicks the profile button then the Logout menu item.
        Swallows exceptions so a failed logout does not crash a test teardown.
        """
        try:
            page.get_by_role("button", name="Profileadmin").click()
            page.get_by_role("menuitem", name="Logout").click()
            page.wait_for_load_state("networkidle")
        except Exception as exc:  # noqa: BLE001
            print(f"[OrcClient.logout] Warning: logout failed — {exc}")

    # ------------------------------------------------------------------
    # Browser factory
    # ------------------------------------------------------------------

    def new_browser(self, pw, headless: bool = False):
        """Launch a Chromium browser and return (browser, context).

        Ignores TLS certificate errors so self-signed ORC certs do not block
        automation.

        Parameters
        ----------
        pw : playwright.sync_api.Playwright
            The Playwright instance passed in from ``sync_playwright()``.
        headless : bool
            False  → visible window (default).
            True   → Chrome new headless (``--headless=new``): full GPU +
                     WebRTC pipeline preserved. Playwright is launched with
                     ``headless=False`` so it doesn't strip the rendering
                     stack; the Chrome arg does the actual headless switch.

        Returns
        -------
        tuple[Browser, BrowserContext]
        """
        extra_args: list[str] = []
        if headless:
            # --headless=new preserves the full rendering pipeline (GPU,
            # WebRTC, canvas). Pass as a Chrome arg and keep Playwright's
            # headless=False so the rendering stack isn't stripped.
            extra_args.append("--headless=new")
        launch_headless = False

        browser = pw.chromium.launch(
            headless=launch_headless,
            channel="chrome",
            args=[
                "--ignore-certificate-errors",
                "--start-maximized",
                # Allow media/video autoplay without a user gesture (blocked by
                # default in headless Chrome, which prevents WebRTC players from
                # initialising on the ORC dashboard).
                "--autoplay-policy=no-user-gesture-required",
                # Suppress Chrome's media engagement heuristics so headless mode
                # doesn't silently downgrade video initialisation.
                "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
                # Allow WebRTC in headless mode (disables the "no user media" guard).
                "--use-fake-ui-for-media-stream",
                # Prevent Chrome from throttling JavaScript timers in background
                # tabs. Without these, ORC's JWT token-refresh timer can stall in
                # headless mode, causing the session to expire mid-soak.
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                *extra_args,
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1920, "height": 1080},
        )
        return browser, context
