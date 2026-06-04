"""
config/environments.py
Loads environment configuration from .env file.

Three target servers, all sharing the same ORC app credentials and Windows
Administrator password. Hardware specs drive the stream tier selection.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (parent of config/)
_env_file = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_file, override=False)

# Shared credentials (same across all three servers)
_admin_user     = os.getenv("ORC_ADMIN_USER",     "admin")
_admin_pass     = os.getenv("ORC_ADMIN_PASS",     "")
_non_admin_user = os.getenv("ORC_NON_ADMIN_USER", "manager")
_non_admin_pass = os.getenv("ORC_NON_ADMIN_PASS", "")
_server_user    = os.getenv("ORC_SERVER_USER",    "Administrator")
_server_pass    = os.getenv("ORC_SERVER_PASS",    "")

def _server(name: str, base_url: str, host: str, hardware: str, tiers: list[int]) -> dict:
    return {
        "name":           name,
        "base_url":       base_url,
        "server_host":    host,
        "hardware":       hardware,
        "stream_tiers":   tiers,       # session counts to test on this server
        "admin_user":     _admin_user,
        "admin_pass":     _admin_pass,
        "non_admin_user": _non_admin_user,
        "non_admin_pass": _non_admin_pass,
        "server_user":    _server_user,
        "server_pass":    _server_pass,
    }

# ---------------------------------------------------------------------------
# Server definitions
# ---------------------------------------------------------------------------

SERVERS: dict[str, dict] = {
    "qa155": _server(
        name="orc-qa-155",
        base_url=os.getenv("ORC_QA155_URL", "https://orc-qa-155.actdev.local"),
        host=os.getenv("ORC_QA155_HOST",    "orc-qa-155.actdev.local"),
        hardware="8 core / 16 GB",
        tiers=[12, 24, 36],
    ),
    "qa160": _server(
        name="orc-qa-160",
        base_url=os.getenv("ORC_QA160_URL", "https://orc-qa-160.actdev.local"),
        host=os.getenv("ORC_QA160_HOST",    "orc-qa-160.actdev.local"),
        hardware="4 core / 8 GB",
        tiers=[12, 18, 24],
    ),
    "qa162": _server(
        name="orc-qa-162",
        base_url=os.getenv("ORC_QA162_URL", "https://orc-qa-162.actdev.local"),
        host=os.getenv("ORC_QA162_HOST",    "orc-qa-162.actdev.local"),
        hardware="16 core / 32 GB",
        tiers=[12, 24, 36],
    ),
    "qa172": _server(
        name="orc-qa-172",
        base_url=os.getenv("ORC_QA172_URL", "https://10.101.64.167"),
        host=os.getenv("ORC_QA172_HOST",    "10.101.64.167"),
        hardware="unknown",
        tiers=[12],
    ),
    "qa173": _server(
        name="orc-qa-173",
        base_url=os.getenv("ORC_QA173_URL", "https://orc-qa-173.actdev.local"),
        host=os.getenv("ORC_QA173_HOST",    "orc-qa-173.actdev.local"),
        hardware="32 core / 16 GB",
        tiers=[12, 24, 36],
    ),
    "visions": _server(
        name="orc-visions",
        base_url=os.getenv("ORC_VISIONS_URL", "https://10.101.60.13"),
        host=os.getenv("ORC_VISIONS_HOST",    "10.101.60.13"),
        hardware="unknown",
        tiers=[12],
    ),
}

# Default environment — backward compat with code that imports ENV directly
ENV: dict = SERVERS["qa155"]
