from dataclasses import dataclass
import time


@dataclass
class WebRTCSnapshot:
    timestamp: float      # time.time()
    tab_index: int
    connection_count: int # number of active RTCPeerConnections on this tab
    fps: float            # average FPS across all video inbound-rtp tracks (delta framesDecoded)
    dropped_frames: int   # total dropped frames across all tracks
    bytes_received: int   # total bytes received across all tracks
    jitter_ms: float      # average jitter in milliseconds across all tracks
    rtt_ms: float         # round-trip time in ms from candidate-pair stats (0 if unavailable)
    packets_received: int = 0  # total packets received across all tracks
    packets_lost: int = 0      # total packets lost across all tracks
    per_source_bytes: list = None  # bytes_received per RTCPeerConnection (one PC ≈ one source)


class WebRTCCollector:
    """
    Collects WebRTC statistics from ORC browser tabs via JS injection.

    USAGE:
        collector = WebRTCCollector()

        # BEFORE navigating to the page (or when creating a new page context):
        collector.install(page)  # adds init script to intercept RTCPeerConnection

        # After dashboard is loaded and streams are playing:
        snapshot = collector.collect(page, tab_index=0)

        # Collect from multiple pages:
        snapshots = collector.collect_all(pages)  # pages: list of Page objects
    """

    # JavaScript to inject BEFORE the page loads via page.add_init_script()
    # This intercepts RTCPeerConnection creation to track all instances
    INIT_SCRIPT = """
    (function() {
        window.__orcPCs = [];
        const OrigPC = window.RTCPeerConnection;
        if (!OrigPC) return;
        window.RTCPeerConnection = function(...args) {
            const pc = new OrigPC(...args);
            window.__orcPCs.push(pc);
            return pc;
        };
        Object.setPrototypeOf(window.RTCPeerConnection, OrigPC);
        window.RTCPeerConnection.prototype = OrigPC.prototype;
    })();
    """

    # JavaScript to call via page.evaluate() to get current stats.
    # FPS is computed from delta(framesDecoded)/delta(time) rather than
    # framesPerSecond, because headless Chromium never populates framesPerSecond
    # (no GPU rendering pipeline), but framesDecoded is a real decoder counter
    # that increments even in headless — matching how ORC's own Angular overlay
    # computes fps (which is why orlistcomponent?streamMetrics=true shows real fps).
    COLLECT_SCRIPT = """
    async () => {
        const pcs = window.__orcPCs || [];
        const active = pcs.filter(pc => pc.connectionState !== 'closed' && pc.connectionState !== 'failed');
        const now = performance.now();
        const prev = window.__orcLastStats || {};
        const curr = {};
        const results = [];
        for (let i = 0; i < active.length; i++) {
            const pc = active[i];
            let framesDecoded = 0, dropped = 0, bytesRx = 0, jitter = 0, rtt = 0;
            let packetsReceived = 0, packetsLost = 0;
            try {
                const stats = await pc.getStats();
                stats.forEach(r => {
                    if (r.type === 'inbound-rtp' && r.kind === 'video') {
                        framesDecoded += r.framesDecoded || 0;
                        dropped       += r.framesDropped || 0;
                        bytesRx       += r.bytesReceived || 0;
                        jitter        += (r.jitter || 0) * 1000;
                        packetsReceived += r.packetsReceived || 0;
                        packetsLost     += r.packetsLost || 0;
                    }
                    if (r.type === 'candidate-pair' && r.nominated) {
                        rtt = (r.currentRoundTripTime || 0) * 1000;
                    }
                });
            } catch(e) {}
            curr[i] = {framesDecoded, ts: now};
            // Compute fps from framesDecoded delta — works in headless
            let fps = 0;
            if (prev[i] !== undefined) {
                const dt = (now - prev[i].ts) / 1000;
                if (dt > 0) fps = Math.max(0, (framesDecoded - prev[i].framesDecoded) / dt);
            }
            results.push({fps, dropped, bytesRx, jitter, rtt, packetsReceived, packetsLost});
        }
        window.__orcLastStats = curr;
        return {count: active.length, tracks: results};
    }
    """

    def install(self, page) -> None:
        """
        Install the RTCPeerConnection interceptor on a page.
        MUST be called before page.goto() — uses page.add_init_script().
        Safe to call multiple times (add_init_script is idempotent).
        """
        page.add_init_script(self.INIT_SCRIPT)

    def collect(self, page, tab_index: int = 0) -> WebRTCSnapshot:
        """
        Collect a WebRTC stats snapshot from a single page.
        Evaluates COLLECT_SCRIPT in the main frame AND every child frame so that
        video players embedded in iframes (same- or cross-origin) are captured.
        Aggregates across all active connections and tracks found in any frame.
        Returns a WebRTCSnapshot with aggregated values (all zeros on failure).
        """
        all_tracks: list = []
        total_count: int = 0
        best_frame_tracks: list = []  # per-PC tracks from the frame with most connections
        best_frame_count: int = 0

        # page.frames includes the main frame + all child frames (any depth/origin).
        # Guard against the page being closed/navigated mid-poll (common in long soaks).
        try:
            frames = list(page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                result = frame.evaluate(self.COLLECT_SCRIPT)
                cnt = result.get("count", 0)
                if cnt > 0:
                    tracks = result.get("tracks", [])
                    total_count += cnt
                    all_tracks.extend(tracks)
                    if cnt > best_frame_count:
                        best_frame_count = cnt
                        best_frame_tracks = tracks
            except Exception:
                pass

        # per_source_bytes: bytes_received per RTCPeerConnection from the dominant frame
        # Index i corresponds to the i-th source in room_sources (creation order).
        per_source_bytes = [int(t.get("bytesRx", 0)) for t in best_frame_tracks]

        track_count = len(all_tracks)

        total_fps = sum(t.get("fps", 0) for t in all_tracks)
        total_dropped = sum(t.get("dropped", 0) for t in all_tracks)
        total_bytes = sum(t.get("bytesRx", 0) for t in all_tracks)
        total_jitter = sum(t.get("jitter", 0) for t in all_tracks)
        total_pkts_rx = sum(t.get("packetsReceived", 0) for t in all_tracks)
        total_pkts_lost = sum(t.get("packetsLost", 0) for t in all_tracks)
        # rtt comes from candidate-pair, use last non-zero
        rtt_values = [t.get("rtt", 0) for t in all_tracks if t.get("rtt", 0) > 0]
        rtt_ms = rtt_values[-1] if rtt_values else 0.0

        avg_fps = total_fps / track_count if track_count > 0 else 0.0
        avg_jitter = total_jitter / track_count if track_count > 0 else 0.0

        return WebRTCSnapshot(
            timestamp=time.time(),
            tab_index=tab_index,
            connection_count=total_count,
            fps=avg_fps,
            dropped_frames=int(total_dropped),
            bytes_received=int(total_bytes),
            jitter_ms=avg_jitter,
            rtt_ms=rtt_ms,
            packets_received=int(total_pkts_rx),
            packets_lost=int(total_pkts_lost),
            per_source_bytes=per_source_bytes,
        )

    def collect_all(self, pages: list, delay_between_ms: int = 100) -> list[WebRTCSnapshot]:
        """
        Collect snapshots from all pages. Returns list in same order as pages.
        delay_between_ms: small delay between pages to avoid hammering JS engine.
        """
        snapshots = []
        delay_s = delay_between_ms / 1000.0
        for idx, page in enumerate(pages):
            snapshot = self.collect(page, tab_index=idx)
            snapshots.append(snapshot)
            if idx < len(pages) - 1:
                time.sleep(delay_s)
        return snapshots
