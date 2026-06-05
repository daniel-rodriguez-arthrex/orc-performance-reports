"""
config/sources.py

All stream source definitions used in performance testing.

Each source is a dict with these keys:
    name           str   display name (used as source name in ORC room config)
    source_type    str   ORC source type: "Matrix", "Vision", "Sony SRG-X120",
                         "Sony SRG-300SE", "Sony SRG-A12", "Axis M3085-V", "uHD4"
    url            str   RTSP or RTSPS URL
    bandwidth_spec str   ORC bandwidth dropdown label e.g. "1920x1080@30fps"
    bandwidth_mbps float ORC enforcement Mbps — from bandwidth.json (bandwidthBySpec).
                         This is what ORC counts against the egress cap, NOT the
                         actual network bitrate of the RTSP stream.
    username       str   stream credential username (empty string if not required)
    password       str   stream credential password (empty string if not required)

NOTE — enforcement vs actual bitrate:
    The simulated RTSP server streams at ~2.5 Mbps on the wire (1080p@5fps quality).
    However, ORC enforcement uses the configured bandwidth_spec value from bandwidth.json,
    so a Matrix source configured as 1920x1080@30fps counts as 8 Mbps against the cap
    regardless of actual stream bitrate. bandwidth_mbps here reflects the enforcement value.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

_env_file = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_file, override=False)

# Vision RTSPS device credentials (shared across all Vision units in the QA lab)
_vision_user = os.getenv("ORC_VISION_USER", "arthrex")
_vision_pass = os.getenv("ORC_VISION_PASS", "")

# Physical camera credentials (per-device; set in .env)
_cam_sony1_pass  = os.getenv("ORC_CAM_SONY1_PASS",  "")
_cam_sony2_pass  = os.getenv("ORC_CAM_SONY2_PASS",  "admin")
_cam_sony3_pass  = os.getenv("ORC_CAM_SONY3_PASS",  "")
_cam_axis1_pass  = os.getenv("ORC_CAM_AXIS1_PASS",  "")

# ---------------------------------------------------------------------------
# Simulated RTSP streams
# Fake camera server at rtsp://10.101.64.68:8554/orc/stream[1-100]
# Streams 2–5 are broken on the sim server — use 6+ only.
# Source type: Sony SRG-X120 with 1280x720@15fps spec = 3 Mbps (per bandwidth.json).
# The RTSP URL is a sim server — source type only affects which bandwidth specs
# are available in the ORC dropdown. Actual wire bitrate ~2.5 Mbps.
# ORC enforcement value: 3 Mbps.
# ---------------------------------------------------------------------------
SIMULATED: list[dict] = [
    {
        "name":           f"Sim {i:02d}",
        "source_type":    "Sony SRG-X120",
        "url":            f"rtsp://10.101.64.68:8554/orc/stream{i}",
        "bandwidth_spec": "1280x720@15fps",
        "bandwidth_mbps": 3,    # enforcement value per bandwidth.json
        "username":       "",
        "password":       "",
    }
    for i in range(6, 42)   # streams 6–41; skips 2–5 which are broken on the sim server
]

# ---------------------------------------------------------------------------
# Vision RTSPS sources  (12 Mbps, 1080p60)
# Devices must have RTSPS streaming enabled. Credentials read from .env
# (ORC_VISION_USER / ORC_VISION_PASS).
# 6 confirmed Vision devices available for perf testing on orc-qa-155.
# NOTE: Only 6 physical Vision units available.
#       For 8+ session coverage, use --setup-sources simulated or mixed with capacity_validation.
# ---------------------------------------------------------------------------
VISION: list[dict] = [
    {
        "name":           "Vision 44-230",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.44.230:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    {
        "name":           "Vision 72-28",
        "source_type":    "Vision",
        "url":            "rtsps://10.72.83.28:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    {
        "name":           "Vision 42-124",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.42.124:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    {
        "name":           "Vision 42-136",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.42.136:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    {
        "name":           "Vision 42-131",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.42.131:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    {
        "name":           "Vision 60-133",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.60.133:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
    # Inactive - not used (index 6+, only first 6 are selected). Re-enable by moving above Vision 60-133.
    {
        "name":           "Vision 44-170",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.44.170:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },    
    {
        "name":           "Vision 42-130",
        "source_type":    "Vision",
        "url":            "rtsps://10.101.42.130:8554/primarycamera",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
        "username":       _vision_user,
        "password":       _vision_pass,
    },
]

# ---------------------------------------------------------------------------
# Physical Sony / Axis / uHD4 cameras
# ---------------------------------------------------------------------------
CAMERAS: list[dict] = [
    {
        "name":           "Sony Camera 1",
        "source_type":    "Sony SRG-X120",
        "url":            "rtsp://10.101.60.135/media/video1",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 10,
        "username":       "admin",
        "password":       _cam_sony1_pass,
    },
    {
        "name":           "Sony Camera 2",
        "source_type":    "Sony SRG-300SE",
        "url":            "rtsp://10.101.60.136/media/video1",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 10,
        "username":       "admin",
        "password":       _cam_sony2_pass,
    },
    {
        "name":           "Sony Camera 3",
        "source_type":    "Sony SRG-A12",
        "url":            "rtsp://10.101.60.138/media/video1",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 10,
        "username":       "admin",
        "password":       _cam_sony3_pass,
    },
    {
        "name":           "Axis Camera 1",
        "source_type":    "Axis M3085-V",
        "url":            "rtsp://10.101.60.142/axis-media/media.amp",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 10,
        "username":       "root",
        "password":       _cam_axis1_pass,
    },
    {
        "name":           "UHD4 - 44.235",
        "source_type":    "uHD4",
        "url":            "rtsp://10.101.44.235/CameraVideo",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 8,
        "username":       "",
        "password":       "",
    },
]

# ---------------------------------------------------------------------------
# Matrix sources  (RTMP ingest, 8 Mbps, 1080p@30fps)
# ---------------------------------------------------------------------------
MATRIX: list[dict] = [
    {
        "name":           "Matrix Extron Room 1",
        "source_type":    "Matrix",
        "url":            "rtmp://10.101.64.163:1935/matrix/room1",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 8,
        "username":       "",
        "password":       "",
    },
]

# ---------------------------------------------------------------------------
# Convenience groupings
# ---------------------------------------------------------------------------
ALL_SOURCES: list[dict] = SIMULATED + VISION + CAMERAS + MATRIX
