from dataclasses import dataclass, field
import time


@dataclass
class ApiCall:
    timestamp: float          # time.time() when request was initiated
    url: str
    method: str
    duration_ms: float        # time from request start to response received
    status: int               # HTTP status code
    is_bandwidth_check: bool  # True if URL contains bandwidth-related patterns


class ApiLatencyMonitor:
    """
    Monitors HTTP request/response timing on a Playwright page.

    Focuses on the ORC bandwidth validation API calls, which fire on every
    layout change and dashboard load. Also captures all other API calls
    for general latency analysis.

    USAGE:
        monitor = ApiLatencyMonitor()
        monitor.attach(page)          # call after page is created, before navigation
        # ... navigate, interact ...
        calls = monitor.get_calls()   # list of all captured ApiCall objects
        bw_calls = monitor.get_bandwidth_calls()  # only bandwidth-check calls
        monitor.clear()               # reset for next scenario
    """

    # URL patterns that indicate a bandwidth check API call
    BANDWIDTH_PATTERNS = [
        "/bandwidth",
        "/api/bandwidth",
        "bandwidth-check",
        "streamCapacity",
        "stream-capacity",
        "egress",
    ]

    # ORC server-side API paths worth measuring for latency.
    # Excludes static assets, WebRTC negotiation, client-side telemetry, and
    # Angular routes which dominate the raw call list but aren't server API calls.
    ORC_API_PATHS = (
        "/graphql",
        "/graphql-sso",
        "/token/generate",
        "/api/",
    )

    def __init__(self):
        self._calls: list[ApiCall] = []

    def attach(self, page) -> None:
        """
        Attach request/requestfinished listeners to a Playwright page.
        Call once per page, before any navigation.

        Two-handler approach: record wall-clock start time in 'request', then
        compute duration in 'requestfinished'. Avoids calling request.timing()
        or request.response() inside an event callback, which can raise in
        Playwright's sync API due to internal threading constraints.
        """
        monitor = self
        # Map id(request) → wall-clock start time
        _start_times: dict[int, float] = {}

        def on_request(request):
            try:
                _start_times[id(request)] = time.time()
            except Exception:
                pass

        def on_request_finished(request):
            try:
                start = _start_times.pop(id(request), None)
                duration_ms = (time.time() - start) * 1000 if start is not None else 0.0
                # request.url and request.method are direct attributes — safe to read
                call = ApiCall(
                    timestamp=start if start is not None else time.time(),
                    url=request.url,
                    method=request.method,
                    duration_ms=duration_ms,
                    status=0,   # response.status requires an extra Playwright call; skip
                    is_bandwidth_check=monitor._is_bandwidth_check(request.url),
                )
                monitor._calls.append(call)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("requestfinished", on_request_finished)

    def get_calls(self) -> list[ApiCall]:
        """Return all captured API calls (copy)."""
        return list(self._calls)

    def get_orc_api_calls(self) -> list[ApiCall]:
        """Return only calls matching ORC_API_PATHS — excludes static assets,
        WebRTC negotiation (/rtc/v1/whep/), telemetry, and Angular routes."""
        from urllib.parse import urlparse
        return [
            c for c in self._calls
            if any(urlparse(c.url).path.startswith(p) for p in self.ORC_API_PATHS)
        ]

    def get_bandwidth_calls(self) -> list[ApiCall]:
        """Return only calls matching BANDWIDTH_PATTERNS in their URL."""
        return [c for c in self._calls if c.is_bandwidth_check]

    def clear(self) -> None:
        """Clear all recorded calls."""
        self._calls.clear()

    def summary(self) -> dict:
        """
        Return a summary dict:
        {
            "total_calls": int,
            "bandwidth_calls": int,
            "avg_duration_ms": float,
            "max_duration_ms": float,
            "p95_duration_ms": float,
            "bandwidth_avg_ms": float,
            "bandwidth_p95_ms": float,
        }
        Returns zeros if no calls recorded.
        """
        all_calls = self._calls
        bw_calls = [c for c in all_calls if c.is_bandwidth_check]

        def _p95(durations: list) -> float:
            if not durations:
                return 0.0
            sorted_d = sorted(durations)
            idx = int(len(sorted_d) * 0.95)
            idx = min(idx, len(sorted_d) - 1)
            return sorted_d[idx]

        def _avg(durations: list) -> float:
            return sum(durations) / len(durations) if durations else 0.0

        all_durations = [c.duration_ms for c in all_calls]
        bw_durations = [c.duration_ms for c in bw_calls]

        return {
            "total_calls": len(all_calls),
            "bandwidth_calls": len(bw_calls),
            "avg_duration_ms": _avg(all_durations),
            "max_duration_ms": max(all_durations) if all_durations else 0.0,
            "p95_duration_ms": _p95(all_durations),
            "bandwidth_avg_ms": _avg(bw_durations),
            "bandwidth_p95_ms": _p95(bw_durations),
        }

    def _is_bandwidth_check(self, url: str) -> bool:
        """Check if the URL matches any bandwidth check pattern."""
        return any(pattern in url for pattern in self.BANDWIDTH_PATTERNS)
