"""
source_setup.py — Room source configuration via the ORC GraphQL API.

Provides SourceSetup, which uses direct GraphQL calls (no browser) to
configure video sources for one or more rooms.

Example
-------
from core.source_setup import SourceSetup

setup = SourceSetup(env)  # env dict from config/environments.py
results = setup.configure_rooms([
    {"room_name": "OR 01", "source": SIMULATED[0], "make_primary": True},
    {"room_name": "OR 02", "source": SIMULATED[1], "make_primary": True},
])
# results: {"OR 01": "configured", "OR 02": "configured", ...}
"""

from __future__ import annotations

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_GQL_PATH   = "/graphql"
_LOGIN_PATH = "/login"

# ---------------------------------------------------------------------------
# Pre-defined simulated source catalogue (kept for backwards compat)
# ---------------------------------------------------------------------------

SIMULATED: list[dict] = [
    {
        "name": "Sim-1080p30",
        "type": "Matrix",
        "url": "rtsp://10.0.0.1/stream1",
        "username": "",
        "password": "",
        "bandwidth_spec": "1920x1080@30fps",
        "bandwidth_mbps": 8,
    },
    {
        "name": "Sim-1080p60",
        "type": "Matrix",
        "url": "rtsp://10.0.0.1/stream2",
        "username": "",
        "password": "",
        "bandwidth_spec": "1920x1080@60fps",
        "bandwidth_mbps": 12,
    },
]


class SourceSetup:
    """Configures room sources in ORC via the GraphQL API.

    No browser required — all calls go directly to /graphql.

    Usage
    -----
    setup = SourceSetup(env)
    results = setup.configure_rooms([
        {"room_name": "OR 01", "source": source_dict, "make_primary": True},
    ])
    """

    def __init__(self, env_or_client) -> None:
        """Accept either an env dict or a legacy OrcClient (ignored — we use env directly)."""
        if isinstance(env_or_client, dict):
            env = env_or_client
        else:
            # Legacy: OrcClient passed — pull env from it
            env = {
                "base_url":   env_or_client.base_url,
                "admin_user": env_or_client.username,
                "admin_pass": env_or_client.password,
            }
        self._base_url = env["base_url"].rstrip("/")
        self._username = env.get("admin_user") or env.get("username", "admin")
        self._password = env.get("admin_pass") or env.get("password", "")
        self._session  = requests.Session()
        self._session.verify = False
        self._logged_in = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure_rooms(self, pw_or_rooms, room_sources=None) -> dict:
        """Configure sources for each room.

        Accepts two call signatures for backward compatibility:
            configure_rooms(pw, room_sources)   — legacy (pw is ignored)
            configure_rooms(room_sources)        — new direct form
        """
        if room_sources is None:
            # New form: first arg IS room_sources
            room_sources = pw_or_rooms

        self._ensure_login()

        # Build a name->id map of all rooms
        room_map = self._get_room_map()

        results: dict[str, str] = {}
        for entry in room_sources:
            room_name: str  = entry["room_name"]
            source: dict    = entry["source"]

            room_id = room_map.get(room_name)
            if not room_id:
                results[room_name] = f"error: room '{room_name}' not found on server"
                print(f"[SourceSetup] ERROR: room '{room_name}' not found")
                continue

            try:
                result = self._configure_single_room(room_id, room_name, source)
                results[room_name] = result
            except Exception as exc:  # noqa: BLE001
                results[room_name] = f"error: {exc}"
                print(f"[SourceSetup] ERROR configuring '{room_name}': {exc}")

        # Push all room configs to SRS in one final call
        self._send_config_to_server()

        # Assign all configured rooms to the logged-in admin user so they
        # appear on the dashboard
        configured_rooms = [rn for rn, st in results.items() if st in ("configured", "skipped")]
        if configured_rooms:
            room_ids = [room_map[rn] for rn in configured_rooms if rn in room_map]
            user_id  = self._get_user_id(self._username)
            if user_id:
                self._assign_rooms_to_user(user_id, room_ids)
                print(f"  [SourceSetup] Assigned {len(room_ids)} room(s) to user '{self._username}'.")
            else:
                print(f"  [SourceSetup] WARNING: could not find user ID for '{self._username}' — rooms not assigned.")

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _configure_single_room(self, room_id: str, room_name: str, source: dict) -> str:
        """Delete existing streams on the room and add the new source."""
        # Get existing streams for this room
        existing = self._get_room_streams(room_id)

        # Check if already configured with this source
        for s in existing:
            if s.get("name") == source["name"]:
                print(f"[SourceSetup] '{room_name}': source '{source['name']}' already exists — skipping.")
                return "skipped"

        # Delete any existing streams first
        for s in existing:
            self._delete_stream(s["id"])

        # Create the new stream and retrieve its ID
        new_stream = self._create_stream(room_id, source)
        stream_id = new_stream.get("id")
        # Push stream config to SRS
        if stream_id:
            self._update_stream_srs(stream_id, source)
        # Save the room (name + privacyMode) — required before sendConfigToServer
        self._update_room(room_id, room_name)
        print(f"[SourceSetup] '{room_name}': configured '{source['name']}'")
        return "configured"

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        resp = self._session.post(
            self._base_url + _LOGIN_PATH,
            json={"username": self._username, "password": self._password, "login_type": "local"},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  [SourceSetup] Logged in to {self._base_url} as '{self._username}'")
        self._logged_in = True

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        body = {"query": query}
        if variables:
            body["variables"] = variables
        resp = self._session.post(self._base_url + _GQL_PATH, json=body, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL error: {payload['errors']}")
        return payload.get("data", {})

    def _get_room_map(self) -> dict[str, str]:
        """Return {room_name: room_id} for all rooms on the server."""
        data = self._gql("{ allOperatingRooms { id name } }")
        return {r["name"]: r["id"] for r in data.get("allOperatingRooms", [])}

    def _get_room_streams(self, room_id: str) -> list[dict]:
        """Return list of {id, name} for streams in a room."""
        data = self._gql(
            "query($id:String!){ operatingRoom(id:$id){ streams { id name } } }",
            {"id": room_id},
        )
        return (data.get("operatingRoom") or {}).get("streams") or []

    def _delete_stream(self, stream_id: str) -> None:
        self._gql(
            "mutation($id:String!){ deleteStream(id:$id){ id } }",
            {"id": stream_id},
        )

    def _update_room(self, room_id: str, room_name: str) -> None:
        """Call updateOperatingRoom with name+privacyMode (mirrors what the UI sends on save)."""
        self._gql(
            "mutation($id:String!,$data:OperatingRoomInput!){ updateOperatingRoom(id:$id,data:$data){ id } }",
            {"id": room_id, "data": {"name": room_name, "privacyMode": None}},
        )

    def _update_stream_srs(self, stream_id: str, source: dict) -> None:
        """Call updateStream with shouldUpdateSRSConfig=true to push the stream to SRS."""
        stream_input = {
            "name":        source["name"],
            "rtspurl":     source["url"],
            "username":    source.get("username", ""),
            "password":    source.get("password", ""),
            "privacyMode": False,
            "rank":        0,
        }
        if source.get("bandwidth_mbps") is not None:
            stream_input["bandwidthInMbps"] = float(source["bandwidth_mbps"])
        if source.get("bandwidth_spec") is not None:
            stream_input["bandwidthSpec"] = source["bandwidth_spec"]
        if source.get("type") is not None:
            stream_input["source"] = source["type"]
        self._gql(
            "mutation($streamId:String!,$data:StreamInput!,$srs:Boolean){"
            "  updateStream(streamId:$streamId,data:$data,shouldUpdateSRSConfig:$srs){ id name }"
            "}",
            {"streamId": stream_id, "data": stream_input, "srs": True},
        )

    def _send_config_to_server(self) -> None:
        """Call sendConfigToServer to finalize the SRS config push."""
        self._gql("mutation { sendConfigToServer }")
        print("  [SourceSetup] sendConfigToServer — done.")
    def _get_user_id(self, username: str) -> str | None:
        """Return the user ID for *username*, or None if not found."""
        data = self._gql("{ allUsers { id username } }")
        for u in data.get("allUsers", []):
            if u["username"] == username:
                return u["id"]
        return None

    def _assign_rooms_to_user(self, user_id: str, room_ids: list[str]) -> None:
        """Call assignRoomsToUser so rooms appear on the user's dashboard."""
        self._gql(
            "mutation($id:String!,$data:SettingsInput!){ assignRoomsToUser(id:$id,data:$data){ id } }",
            {"id": user_id, "data": {"rooms": room_ids}},
        )
    def _create_stream(self, room_id: str, source: dict) -> dict:
        stream_input = {
            "name":        source["name"],
            "rtspurl":     source["url"],
            "username":    source.get("username", ""),
            "password":    source.get("password", ""),
            "privacyMode": False,
            "rank":        0,
        }
        if source.get("bandwidth_mbps") is not None:
            stream_input["bandwidthInMbps"] = float(source["bandwidth_mbps"])
        if source.get("bandwidth_spec") is not None:
            stream_input["bandwidthSpec"] = source["bandwidth_spec"]
        if source.get("type") is not None:
            stream_input["source"] = source["type"]
        data = self._gql(
            "mutation($data:StreamInput!,$roomId:String!){ createStream(data:$data,operatingRoomId:$roomId){ id name } }",
            {"data": stream_input, "roomId": room_id},
        )
        return data.get("createStream", {})

