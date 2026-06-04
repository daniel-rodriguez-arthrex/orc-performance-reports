"""
core/room_cleanup.py
Deletes all rooms (and their streams) on an ORC server via the GraphQL API.

Uses the same pattern as the C# AllRoomsSteps / RoomManagement_Steps:
  1. POST {base_url}/login  →  receive session cookie
  2. query  allOperatingRooms  →  get every room + stream ids
  3. mutation deleteStream(id)  for each stream  (streams must go first)
  4. mutation deleteOperatingRoom(id)  for each room

All calls go directly to the GraphQL endpoint — no browser required.
Much faster than UI automation for bulk teardown before a test tier.
"""

from __future__ import annotations

import urllib3
import requests

# Suppress TLS warnings for self-signed ORC certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_GQL_PATH   = "/graphql"
_LOGIN_PATH = "/login"

_ROOM_FIELDS   = "id name"
_STREAM_FIELDS = "id name"

_ALL_ROOMS_QUERY = f"""
query {{
  allOperatingRooms {{
    {_ROOM_FIELDS}
    streams {{ {_STREAM_FIELDS} }}
  }}
}}
"""

_ROOM_SOURCES_QUERY = """
query {
  allOperatingRooms {
    name
    streams {
      name
      rtspurl
      source
      bandwidthInMbps
    }
  }
}
"""

_DELETE_STREAM_MUTATION = """
mutation DeleteStream($id: String!) {{
  deleteStream(id: $id) {{ id }}
}}
"""

_DELETE_ROOM_MUTATION = """
mutation DeleteRoom($id: String!) {{
  deleteOperatingRoom(id: $id) {{ id name }}
}}
"""


class RoomCleanup:
    """Delete all rooms from one ORC server via GraphQL.

    Parameters
    ----------
    env : dict
        Server config dict (same shape as config/environments.py SERVERS entries).
        Required keys: base_url, admin_user, admin_pass.
    """

    def __init__(self, env: dict) -> None:
        self._base_url   = env["base_url"].rstrip("/")
        self._username   = env["admin_user"]
        self._password   = env["admin_pass"]
        self._session    = requests.Session()
        self._session.verify = False   # ORC uses self-signed certs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cleanup_all_rooms(self, dry_run: bool = False) -> dict:
        """Delete every room and its streams.

        Parameters
        ----------
        dry_run : bool
            If True, query and report what *would* be deleted without
            actually issuing any mutations.

        Returns
        -------
        dict with keys:
            rooms_deleted   – count of rooms deleted (or would-be deleted)
            streams_deleted – count of streams deleted
            errors          – list of error strings
        """
        self._login()

        rooms  = self._get_all_rooms()
        errors = []

        total_streams = sum(len(r.get("streams") or []) for r in rooms)
        print(f"  [cleanup] {len(rooms)} rooms / {total_streams} streams on {self._base_url}")

        if dry_run:
            for room in rooms:
                streams = room.get("streams") or []
                print(f"  [cleanup][dry-run] Would delete room '{room['name']}' "
                      f"({len(streams)} stream(s))")
            return {
                "rooms_deleted":   len(rooms),
                "streams_deleted": total_streams,
                "errors":          [],
            }

        deleted_streams = 0
        deleted_rooms   = 0

        for room in rooms:
            room_name = room.get("name", room["id"])

            # Delete streams first (FK constraint)
            for stream in room.get("streams") or []:
                try:
                    self._delete_stream(stream["id"])
                    deleted_streams += 1
                except Exception as exc:  # noqa: BLE001
                    msg = f"stream {stream['id']} in '{room_name}': {exc}"
                    print(f"  [cleanup] ERROR deleting {msg}")
                    errors.append(msg)

            # Delete the room
            try:
                self._delete_room(room["id"])
                deleted_rooms += 1
                print(f"  [cleanup] Deleted room '{room_name}'")
            except Exception as exc:  # noqa: BLE001
                msg = f"room '{room_name}' ({room['id']}): {exc}"
                print(f"  [cleanup] ERROR deleting {msg}")
                errors.append(msg)

        print(f"  [cleanup] Done — {deleted_rooms} rooms, {deleted_streams} streams deleted.")
        return {
            "rooms_deleted":   deleted_rooms,
            "streams_deleted": deleted_streams,
            "errors":          errors,
        }

    def ensure_rooms_exist(self, room_names: list[str]) -> dict:
        """Create any rooms that are missing from the server.

        Parameters
        ----------
        room_names : list[str]
            Desired room names, e.g. ["OR 01", "OR 02", ...].

        Returns
        -------
        dict  {"created": [...], "already_exist": [...], "errors": [...]}
        """
        self._login()
        existing = {r["name"] for r in self._get_all_rooms()}

        created      = []
        already_exist = []
        errors        = []

        for name in room_names:
            if name in existing:
                already_exist.append(name)
                continue
            try:
                self._create_room(name)
                created.append(name)
                print(f"  [room-setup] Created room '{name}'")
            except Exception as exc:  # noqa: BLE001
                msg = f"'{name}': {exc}"
                print(f"  [room-setup] ERROR creating {msg}")
                errors.append(msg)

        if created:
            print(f"  [room-setup] {len(created)} room(s) created, "
                  f"{len(already_exist)} already existed.")
        return {"created": created, "already_exist": already_exist, "errors": errors}

    # ------------------------------------------------------------------
    # Internal helpers (continued)
    # ------------------------------------------------------------------

    def read_room_sources(self) -> list[dict]:
        """Return a list of configured room sources from the live server.

        Each entry has: room, source, url, type, bandwidth_mbps.
        Rooms with no streams are skipped.
        Credentials are stripped from rtspurl before returning.
        """
        import re as _re
        self._login()
        rooms = self._gql(_ROOM_SOURCES_QUERY).get("allOperatingRooms") or []
        result = []
        for room in rooms:
            for stream in room.get("streams") or []:
                url = stream.get("rtspurl") or ""
                # strip embedded credentials
                url = _re.sub(r'(?<=://)([^@]+@)', '', url)
                result.append({
                    "room":          room["name"],
                    "source":        stream.get("name") or url,
                    "url":           url,
                    "type":          stream.get("source") or "",
                    "bandwidth_mbps": stream.get("bandwidthInMbps") or 0,
                })
        return result

    def _create_room(self, name: str) -> dict:
        data = self._gql(
            'mutation($name:String!){createOperatingRoom(data:{name:$name}){id name}}',
            {"name": name},
        )
        return data.get("createOperatingRoom", {})


    def _login(self) -> None:
        """POST to /login with local credentials to get a session cookie."""
        url = self._base_url + _LOGIN_PATH
        payload = {
            "username":   self._username,
            "password":   self._password,
            "login_type": "local",
        }
        resp = self._session.post(url, json=payload, timeout=15)
        if not resp.ok:
            raise RuntimeError(
                f"Login failed ({resp.status_code}): {resp.text[:200]}"
            )
        print(f"  [cleanup] Logged in to {self._base_url} as '{self._username}'")

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query/mutation and return the 'data' key."""
        url  = self._base_url + _GQL_PATH
        body = {"query": query}
        if variables:
            body["variables"] = variables

        resp = self._session.post(url, json=body, timeout=30)
        resp.raise_for_status()

        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL error: {payload['errors']}")
        return payload.get("data", {})

    def _get_all_rooms(self) -> list[dict]:
        data = self._gql(_ALL_ROOMS_QUERY)
        return data.get("allOperatingRooms") or []

    def _delete_stream(self, stream_id: str) -> None:
        self._gql(
            "mutation($id:String!){deleteStream(id:$id){id}}",
            {"id": stream_id},
        )

    def _delete_room(self, room_id: str) -> None:
        self._gql(
            "mutation($id:String!){deleteOperatingRoom(id:$id){id name}}",
            {"id": room_id},
        )
