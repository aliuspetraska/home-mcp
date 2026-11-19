"""Shelly Cloud Control API — relays, plugs, rollers/covers, lights.

Auth is the account-level "Authorization cloud key" plus the server URI that
hosts the account — both shown in the Shelly Cloud app under
User settings -> Authorization cloud key.
Docs: https://shelly-api-docs.shelly.cloud/cloud-control-api/

Endpoints used:
  form-encoded (auth_key in body, {isok, data} envelope):
    POST /interface/device/list  -> device inventory (names, model, gen, online)
    POST /device/all_status      -> last known status of every device
  v2.0-beta (JSON body, auth_key as query param):
    POST /v2/devices/api/get            -> state of 1-10 devices
    POST /v2/devices/api/set/{switch,cover,light,groups} -> control

The API is limited to ~1 request/second — enforced client-side.
"""

import logging
import threading
import time
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.config import ENV_FILE, Settings

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 1.05  # seconds between cloud calls (docs: 1 req/s)
_LIST_TTL = 60  # seconds to cache the device inventory


class ShellyError(ToolError):
    """Shown to the MCP client as a clean tool error message."""


class ShellyClient:
    def __init__(self, settings: Settings) -> None:
        base = settings.shelly_server_url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        self._auth_key = settings.shelly_auth_key
        self._http = httpx.Client(
            base_url=base,
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = _MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _send(self, path: str, **kwargs: Any) -> httpx.Response:
        self._throttle()
        try:
            resp = self._http.post(path, **kwargs)
        except httpx.RequestError as e:
            raise ShellyError(f"Could not reach Shelly Cloud: {e}")
        if resp.status_code in (401, 403):
            raise ShellyError(
                f"Shelly Cloud rejected the auth_key (HTTP {resp.status_code}). "
                f"Check SHELLY_AUTH_KEY in {ENV_FILE} — the key changes when "
                "the account password changes."
            )
        if resp.status_code == 429:
            raise ShellyError("Shelly Cloud rate limit hit (429). Try again shortly.")
        return resp

    def post_form(
        self, path: str, data: dict | None = None, params: dict | None = None
    ) -> Any:
        """Legacy endpoint: form body, {isok, data} envelope. Returns data."""
        resp = self._send(
            path, data={"auth_key": self._auth_key, **(data or {})}, params=params
        )
        if resp.status_code >= 400:
            raise ShellyError(
                f"POST {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        if not payload.get("isok", False):
            raise ShellyError(f"POST {path} failed: {payload.get('errors') or payload}")
        return payload.get("data")

    def post_v2(self, path: str, body: dict) -> Any:
        """v2.0-beta endpoint: JSON body, auth_key as query param."""
        resp = self._send(path, params={"auth_key": self._auth_key}, json=body)
        if resp.status_code != 200:
            err, messages = resp.text[:300], []
            try:
                payload = resp.json()
                err = payload.get("error") or err
                messages = (payload.get("data") or {}).get("messages") or []
            except ValueError:
                pass
            detail = f" ({'; '.join(map(str, messages))})" if messages else ""
            raise ShellyError(f"{path} failed ({resp.status_code}): {err}{detail}")
        try:
            return resp.json()
        except ValueError:
            return None


def register(mcp: FastMCP, settings: Settings) -> None:
    if not settings.shelly_configured:
        logger.warning(
            "SHELLY_SERVER_URL / SHELLY_AUTH_KEY not set — skipping Shelly tools"
        )
        return

    client = ShellyClient(settings)
    devices_cache: list[Any] = [0.0, {}]

    def _devices(fresh: bool = False) -> dict:
        if not fresh and time.time() - devices_cache[0] < _LIST_TTL:
            return devices_cache[1]
        data = client.post_form("/interface/device/list")
        devs = (data or {}).get("devices") or {}
        devices_cache[0], devices_cache[1] = time.time(), devs
        return devs

    def _resolve_id(ref: str) -> str:
        """Accept a device id (hex, case-insensitive, or decimal) or a unique
        device name match; return the canonical hex id."""
        ref_l = str(ref).strip().lower()
        devs = _devices()
        for did in devs:
            if did.lower() == ref_l:
                return did
        if ref_l.isdigit():
            n = int(ref_l)
            for did in devs:
                try:
                    if int(str(did), 16) == n:
                        return did
                except ValueError:
                    pass
        matches = [
            did for did, d in devs.items() if str(d.get("name", "")).lower() == ref_l
        ]
        if not matches:
            matches = [
                did
                for did, d in devs.items()
                if ref_l in str(d.get("name", "")).lower()
            ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ShellyError(
                f"Ambiguous device {ref!r} — matches {matches}. Pass the id."
            )
        raise ShellyError(
            f"No device matching {ref!r}. Call shelly_list_devices for valid "
            "ids/names."
        )

    def _set(path: str, body: dict) -> dict:
        result = client.post_v2(path, body)
        return {"ok": True, "command": body, "api_response": result}

    tags = {"shelly", "devices"}

    @mcp.tool(tags=tags)
    def shelly_list_devices() -> list[dict]:
        """List all Shelly devices on the account — id, name, model (code),
        generation, online state. Call this first to discover ids. All other
        shelly_* tools accept either a device id or a unique case-insensitive
        device name."""
        return [
            {**(info if isinstance(info, dict) else {}), "id": did}
            for did, info in _devices(fresh=True).items()
        ]

    @mcp.tool(tags=tags)
    def shelly_all_status(device_ids: list[str] | None = None) -> dict:
        """Current (or last known) status of every device in one call —
        switch outputs, power readings, sensors. Optionally restrict to
        specific device ids/names."""
        data = client.post_form("/device/all_status", params={"show_info": "true"})
        status = (data or {}).get("devices_status") or {}
        if device_ids:
            ids = {_resolve_id(d).lower() for d in device_ids}
            status = {k: v for k, v in status.items() if k.lower() in ids}
        return status

    @mcp.tool(tags=tags)
    def shelly_device_state(
        device_ids: list[str],
        select: list[str] | None = None,
        pick_status: list[str] | None = None,
        pick_settings: list[str] | None = None,
    ) -> list[dict]:
        """Fetch state for 1-10 devices via the v2 API (basic info plus
        optionally full status and settings).

        Args:
            device_ids: 1-10 device ids or names.
            select: additional data to fetch — any of "status", "settings"
                (default: ["status"]).
            pick_status: restrict status to these top-level properties
                (e.g. ["switch:0", "sys"]); all if omitted.
            pick_settings: restrict settings to these top-level properties."""
        if not 1 <= len(device_ids) <= 10:
            raise ShellyError("device_ids must contain 1-10 ids.")
        select = select or ["status"]
        bad = set(select) - {"status", "settings"}
        if bad:
            raise ShellyError(
                f"select must contain only 'status'/'settings', got {sorted(bad)}"
            )
        body: dict[str, Any] = {
            "ids": [_resolve_id(d) for d in device_ids],
            "select": select,
        }
        pick = {}
        if pick_status:
            pick["status"] = pick_status
        if pick_settings:
            pick["settings"] = pick_settings
        if pick:
            body["pick"] = pick
        result = client.post_v2("/v2/devices/api/get", body)
        return result if isinstance(result, list) else [result]

    write_annotations = {"destructiveHint": True, "idempotentHint": True}

    @mcp.tool(tags=tags, annotations=write_annotations)
    def shelly_switch_set(
        device_id: str,
        on: bool,
        channel: int = 0,
        toggle_after: float | None = None,
    ) -> dict:
        """Turn a relay/plug output on or off (all device generations).

        Args:
            device_id: device id or name.
            on: target output state.
            channel: output channel (default 0).
            toggle_after: seconds until the output flips to the opposite of
                `on` (e.g. on=true + toggle_after=60 -> on for one minute)."""
        body: dict[str, Any] = {
            "id": _resolve_id(device_id),
            "channel": channel,
            "on": on,
        }
        if toggle_after is not None:
            body["toggle_after"] = toggle_after
        return _set("/v2/devices/api/set/switch", body)

    @mcp.tool(tags=tags, annotations=write_annotations)
    def shelly_cover_set(
        device_id: str,
        position: str | int | None = None,
        relative: float | None = None,
        duration: float | None = None,
        channel: int = 0,
        slat_position: int | None = None,
        slat_relative: int | None = None,
    ) -> dict:
        """Move a roller / cover / blind.

        Args:
            device_id: device id or name.
            position: "open", "close", "stop", or target percent 0-100
                (mutually exclusive with relative).
            relative: relative move in %, -100..100.
            duration: stop after N seconds — only valid together with
                position "open"/"close"/"stop".
            slat_position: absolute slat tilt 0-100 (exclusive with
                slat_relative).
            slat_relative: relative slat tilt -100..100.
            channel: cover channel (default 0)."""
        if (
            position is None
            and relative is None
            and slat_position is None
            and slat_relative is None
        ):
            raise ShellyError(
                "Nothing to do — pass position, relative or a slat value."
            )
        if position is not None and relative is not None:
            raise ShellyError("position and relative are mutually exclusive.")
        if slat_position is not None and slat_relative is not None:
            raise ShellyError("slat_position and slat_relative are mutually exclusive.")
        if duration is not None and position not in ("open", "close", "stop"):
            raise ShellyError(
                "duration only applies when position is 'open', 'close' or 'stop'."
            )
        body: dict[str, Any] = {"id": _resolve_id(device_id), "channel": channel}
        if isinstance(position, str):
            body["position"] = position
        elif position is not None:
            if not 0 <= float(position) <= 100:
                raise ShellyError("position percent must be 0-100.")
            body["position"] = int(position)
        if relative is not None:
            if not -100 <= relative <= 100:
                raise ShellyError("relative must be between -100 and 100.")
            body["relative"] = relative
        if duration is not None:
            body["duration"] = duration
        if slat_position is not None:
            if not 0 <= slat_position <= 100:
                raise ShellyError("slat_position must be 0-100.")
            body["slatPosition"] = slat_position
        if slat_relative is not None:
            if not -100 <= slat_relative <= 100:
                raise ShellyError("slat_relative must be between -100 and 100.")
            body["slatRelative"] = slat_relative
        return _set("/v2/devices/api/set/cover", body)

    @mcp.tool(tags=tags, annotations=write_annotations)
    def shelly_light_set(
        device_id: str,
        channel: int = 0,
        on: bool | None = None,
        toggle_after: float | None = None,
        mode: str | None = None,
        brightness: int | None = None,
        temperature: int | None = None,
        red: int | None = None,
        green: int | None = None,
        blue: int | None = None,
        white: int | None = None,
        gain: int | None = None,
        effect: int | None = None,
    ) -> dict:
        """Control a light (not every device supports rgb/temperature).

        Args:
            device_id: device id or name.
            on: output state — required if no other parameter is given.
            toggle_after: seconds until the output flips to the opposite of `on`.
            mode: "color" or "white" (if the device supports both).
            brightness: 0-100 (white mode).
            temperature: 2700-7000 K (white mode).
            red/green/blue/white: 0-255 (color mode).
            gain: 0-100 (color mode).
            effect: 0-6, animated effect if supported.
            channel: light channel (default 0)."""
        params = {
            "on": on,
            "toggle_after": toggle_after,
            "mode": mode,
            "brightness": brightness,
            "temperature": temperature,
            "red": red,
            "green": green,
            "blue": blue,
            "white": white,
            "gain": gain,
            "effect": effect,
        }
        if all(v is None for v in params.values()):
            raise ShellyError(
                "Nothing to do — pass 'on' and/or another light parameter."
            )
        if mode is not None and mode not in ("color", "white"):
            raise ShellyError("mode must be 'color' or 'white'.")
        ranges = {
            "brightness": (0, 100),
            "temperature": (2700, 7000),
            "red": (0, 255),
            "green": (0, 255),
            "blue": (0, 255),
            "white": (0, 255),
            "gain": (0, 100),
            "effect": (0, 6),
        }
        for name, (lo, hi) in ranges.items():
            v = params[name]
            if v is not None and not lo <= v <= hi:
                raise ShellyError(f"{name} must be {lo}-{hi}.")
        body: dict[str, Any] = {
            "id": _resolve_id(device_id),
            "channel": channel,
            **{k: v for k, v in params.items() if v is not None},
        }
        return _set("/v2/devices/api/set/light", body)

    @mcp.tool(tags=tags, annotations=write_annotations)
    def shelly_group_set(
        kind: str,
        ids: list[str],
        command: dict,
    ) -> dict:
        """Control several same-type devices in one call.

        Args:
            kind: "switch", "cover" or "light".
            ids: targets as "<device_id>_<channel>" — channel optional
                (defaults to 0), the device part may be a name.
            command: same fields as the single-device commands, e.g.
                {"on": true} for switches, {"position": 50} for covers,
                {"on": true, "brightness": 40} for lights.
        Returns per-device failures in failedCommands, if any."""
        if kind not in ("switch", "cover", "light"):
            raise ShellyError("kind must be 'switch', 'cover' or 'light'.")
        resolved = []
        for ref in ids:
            dev_ref, sep, ch = str(ref).rpartition("_")
            if not sep or not ch.isdigit():
                dev_ref, ch = str(ref), "0"
            resolved.append(f"{_resolve_id(dev_ref)}_{ch}")
        result = client.post_v2(
            "/v2/devices/api/set/groups",
            {kind: {"ids": resolved, "command": command}},
        )
        failed = (result or {}).get("failedCommands") or {}
        return {
            "ok": not failed,
            "targets": resolved,
            "failed_commands": failed,
            "api_response": result,
        }
