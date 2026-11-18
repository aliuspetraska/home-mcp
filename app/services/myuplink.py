"""myUplink — NIBE heat pump cloud API.

OAuth2 client-credentials flow. Register an application at
https://dev.myuplink.com/ to obtain a client ID and secret.
API docs: https://dev.myuplink.com/apis

Write tools (nibe_set_point, nibe_set_smart_home_mode) need a myUplink Premium
subscription and are not registered when myuplink_read_only=True in Settings.
"""

import logging
import time
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.config import ENV_FILE, Settings

logger = logging.getLogger(__name__)


class MyUplinkError(ToolError):
    """Shown to the MCP client as a clean tool error message."""


class MyUplinkClient:
    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.myuplink_client_id
        self._client_secret = settings.myuplink_client_secret
        self._token_url = settings.myuplink_token_url
        self._scopes = settings.myuplink_scopes
        self._http = httpx.Client(
            base_url=settings.myuplink_base_url,
            timeout=settings.http_timeout_seconds,
            headers={
                "Accept": "application/json",
                "Accept-Language": settings.myuplink_language,
            },
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        if not self._client_id or not self._client_secret:
            raise MyUplinkError(
                "MYUPLINK_CLIENT_ID / MYUPLINK_CLIENT_SECRET are not set. "
                f"Add them to {ENV_FILE}."
            )
        resp = httpx.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scopes,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise MyUplinkError(
                f"Token request failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload.get("expires_in", 3600)
        return self._token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        for attempt in (1, 2):
            headers["Authorization"] = f"Bearer {self._get_token()}"
            resp = self._http.request(method, path, headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 1:
                self._token = None  # force refresh and retry once
                continue
            break
        if resp.status_code == 403:
            raise MyUplinkError(
                "myUplink refused the request (403). Writing settings needs a "
                "myUplink Premium subscription and the WRITESYSTEM scope. "
                "Details: " + resp.text[:300]
            )
        if resp.status_code == 429:
            raise MyUplinkError(
                "myUplink rate limit hit (429). Wait a minute and try again."
            )
        if resp.status_code >= 400:
            raise MyUplinkError(
                f"{method} {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params)


_SYSTEMS_TTL = 300  # seconds — stay within rate limits


def _scale(p: dict) -> float:
    """myUplink reports min/max/step raw while `value` is scaled by scaleValue
    (e.g. 0.1 → min 50 really means 5.0). PATCH expects the scaled value."""
    try:
        return float(p.get("scaleValue") or 1)
    except (TypeError, ValueError):
        return 1.0


def _compact_point(p: dict) -> dict:
    """Trim a raw data point to what is useful for an MCP client."""
    scale = _scale(p)
    out: dict[str, Any] = {
        "id": p.get("parameterId"),
        "name": p.get("parameterName"),
        "value": p.get("value"),
        "unit": p.get("parameterUnit") or None,
        "category": p.get("category"),
    }
    if p.get("writable"):
        out["writable"] = True
        for k_src, k_dst in (
            ("minValue", "min"),
            ("maxValue", "max"),
            ("stepValue", "step"),
        ):
            if p.get(k_src) is not None:
                out[k_dst] = p[k_src] * scale
    enums = p.get("enumValues") or []
    if enums:
        out["options"] = {e.get("value"): e.get("text") for e in enums}
        cur = next(
            (
                e.get("text")
                for e in enums
                if str(e.get("value")) == str(p.get("value"))
            ),
            None,
        )
        if cur:
            out["value_text"] = cur
    if p.get("timestamp"):
        out["updated"] = p["timestamp"]
    return {k: v for k, v in out.items() if v is not None}


def _validate(point: dict, value: str) -> str:
    """Check a new value against the point's metadata; return the value to send."""
    if not point.get("writable"):
        raise MyUplinkError(
            f"Parameter {point.get('parameterId')} ({point.get('parameterName')}) "
            "is not writable."
        )
    enums = point.get("enumValues") or []
    if enums:
        by_value = {str(e.get("value")): e for e in enums}
        by_text = {str(e.get("text", "")).lower(): e for e in enums}
        if value in by_value:
            return value
        if value.lower() in by_text:
            return str(by_text[value.lower()]["value"])
        opts = {e.get("value"): e.get("text") for e in enums}
        raise MyUplinkError(f"Invalid option {value!r}. Allowed: {opts}")
    try:
        num = float(value)
    except ValueError:
        return value  # non-numeric, non-enum: let the API decide
    scale = _scale(point)
    lo = point.get("minValue")
    hi = point.get("maxValue")
    if lo is not None:
        lo = float(lo) * scale
    if hi is not None:
        hi = float(hi) * scale
    if lo is not None and num < float(lo):
        raise MyUplinkError(
            f"{num} is below the minimum {lo} {point.get('parameterUnit', '')}."
        )
    if hi is not None and num > float(hi):
        raise MyUplinkError(
            f"{num} is above the maximum {hi} {point.get('parameterUnit', '')}."
        )
    return value


def register(mcp: FastMCP, settings: Settings) -> None:
    if not settings.myuplink_configured:
        logger.warning(
            "myUplink credentials not set (MYUPLINK_CLIENT_ID / MYUPLINK_CLIENT_SECRET) "
            "— skipping NIBE tools"
        )
        return

    client = MyUplinkClient(settings)
    systems_cache: list[Any] = [0.0, []]

    def _systems(fresh: bool = False) -> list[dict]:
        if not fresh and time.time() - systems_cache[0] < _SYSTEMS_TTL:
            return systems_cache[1]
        data = client.get("/v2/systems/me")
        systems = data.get("systems", []) if isinstance(data, dict) else []
        systems_cache[0], systems_cache[1] = time.time(), systems
        return systems

    def _resolve_device(device_id: str | None) -> str:
        if device_id:
            return device_id
        devices = [d["id"] for s in _systems() for d in s.get("devices", [])]
        if len(devices) == 1:
            return devices[0]
        if not devices:
            raise MyUplinkError("No devices found on this myUplink account.")
        raise MyUplinkError(
            f"Several devices found, pass device_id explicitly: {devices}"
        )

    def _resolve_system(system_id: str | None) -> str:
        if system_id:
            return system_id
        systems = [s["systemId"] for s in _systems()]
        if len(systems) == 1:
            return systems[0]
        if not systems:
            raise MyUplinkError("No systems found on this myUplink account.")
        raise MyUplinkError(
            f"Several systems found, pass system_id explicitly: {systems}"
        )

    tags = {"heatpump", "nibe", "myuplink"}

    @mcp.tool(tags=tags)
    def nibe_list_systems() -> list[dict]:
        """List the myUplink systems (installations) and devices (heat pumps) on the
        account, including system_id, device_id, product name, connection state and
        alarm flag. Call this first to discover IDs."""
        return [
            {
                "system_id": s.get("systemId"),
                "name": s.get("name"),
                "has_alarm": s.get("hasAlarm"),
                "security_level": s.get("securityLevel"),
                "country": s.get("country"),
                "devices": [
                    {
                        "device_id": d.get("id"),
                        "product": (d.get("product") or {}).get("name"),
                        "serial": (d.get("product") or {}).get("serialNumber"),
                        "connection_state": d.get("connectionState"),
                        "firmware": d.get("currentFwVersion"),
                    }
                    for d in s.get("devices", [])
                ],
            }
            for s in _systems(fresh=True)
        ]

    @mcp.tool(tags=tags)
    def nibe_system_details(system_id: str | None = None) -> dict:
        """Get raw details for a myUplink system: devices, zones, alarm status.
        system_id can be omitted if the account has only one system."""
        return client.get(f"/v2/systems/{_resolve_system(system_id)}")

    @mcp.tool(tags=tags)
    def nibe_device_info(device_id: str | None = None) -> dict:
        """Get details for a heat pump: product, firmware, connection state and
        available features. device_id can be omitted if the account has only one
        device."""
        return client.get(f"/v2/devices/{_resolve_device(device_id)}")

    @mcp.tool(tags=tags)
    def nibe_device_points(
        device_id: str | None = None,
        parameter_ids: list[str] | None = None,
        search: str | None = None,
        category: str | None = None,
        writable_only: bool = False,
        limit: int = 150,
    ) -> dict:
        """Read current values (data points) from the heat pump — temperatures,
        compressor status, hot water, degree minutes, settings, etc.

        Args:
            device_id: omit if there is only one device.
            parameter_ids: fetch only these parameter IDs (e.g. ["40004", "40013"]).
            search: case-insensitive substring filter on the parameter name
                (e.g. "hot water", "outdoor", "supply").
            category: filter on category name (substring, case-insensitive).
            writable_only: only return settings that can be changed with
                nibe_set_point.
            limit: maximum number of points to return (default 150).

        Each point has id, name, value, unit; writable points include min/max/step
        and enum points include options (value -> text)."""
        dev = _resolve_device(device_id)
        params = {"parameters": ",".join(parameter_ids)} if parameter_ids else None
        raw = client.get(f"/v2/devices/{dev}/points", params=params)
        points = raw if isinstance(raw, list) else []
        if search:
            s = search.lower()
            points = [p for p in points if s in str(p.get("parameterName", "")).lower()]
        if category:
            c = category.lower()
            points = [p for p in points if c in str(p.get("category", "")).lower()]
        if writable_only:
            points = [p for p in points if p.get("writable")]
        total = len(points)
        return {
            "device_id": dev,
            "total_matching": total,
            "returned": min(total, limit),
            "points": [_compact_point(p) for p in points[:limit]],
        }

    @mcp.tool(tags=tags)
    def nibe_get_alarms(
        system_id: str | None = None,
        only_active: bool = True,
        page: int = 1,
        items_per_page: int = 20,
    ) -> dict:
        """Get alarms/notifications for a system. By default only active alarms;
        set only_active=False to see alarm history."""
        sys_id = _resolve_system(system_id)
        suffix = "/active" if only_active else ""
        data = client.get(
            f"/v2/systems/{sys_id}/notifications{suffix}",
            params={"page": page, "itemsPerPage": items_per_page},
        )
        notes = (data or {}).get("notifications", [])
        return {
            "system_id": sys_id,
            "total": (data or {}).get("numItems"),
            "page": (data or {}).get("page"),
            "alarms": [
                {
                    "alarm_number": n.get("alarmNumber"),
                    "header": n.get("header"),
                    "description": n.get("description"),
                    "severity": n.get("severity"),
                    "status": n.get("status"),
                    "created": n.get("createdDatetime"),
                    "equipment": n.get("equipName"),
                    "device_id": n.get("deviceId"),
                }
                for n in notes
            ],
        }

    @mcp.tool(tags=tags)
    def nibe_get_smart_home_mode(system_id: str | None = None) -> dict:
        """Get the current smart home mode of a system
        (e.g. Default, Normal, Away, Vacation)."""
        return client.get(f"/v2/systems/{_resolve_system(system_id)}/smart-home-mode")

    if settings.myuplink_read_only:
        logger.info("myuplink_read_only set — NIBE write tools not registered")
        return

    write_annotations = {"destructiveHint": True, "idempotentHint": True}

    @mcp.tool(tags=tags, annotations=write_annotations)
    def nibe_set_point(
        parameter_id: str, value: str, device_id: str | None = None
    ) -> dict:
        """Change a heat pump setting (requires myUplink Premium).

        Before calling, use nibe_device_points (writable_only=True or
        parameter_ids=[...]) to confirm the parameter, its current value and
        allowed range/options, and confirm the change with the user. For enum
        settings, value may be the option number or its text. Returns the old
        value, the new value and the API's response."""
        dev = _resolve_device(device_id)
        raw = client.get(
            f"/v2/devices/{dev}/points", params={"parameters": parameter_id}
        )
        matches = [
            p for p in (raw or []) if str(p.get("parameterId")) == str(parameter_id)
        ]
        if not matches:
            raise MyUplinkError(f"Parameter {parameter_id} not found on device {dev}.")
        point = matches[0]
        send = _validate(point, str(value))
        result = client.request(
            "PATCH",
            f"/v2/devices/{dev}/points",
            json={str(parameter_id): send},
            headers={"Content-Type": "application/json-patch+json"},
        )
        return {
            "device_id": dev,
            "parameter_id": parameter_id,
            "name": point.get("parameterName"),
            "old_value": point.get("value"),
            "new_value": send,
            "unit": point.get("parameterUnit") or None,
            "api_response": result,
            "note": "The heat pump may take up to a few minutes to report the new value.",
        }

    @mcp.tool(tags=tags, annotations=write_annotations)
    def nibe_set_smart_home_mode(mode: str, system_id: str | None = None) -> dict:
        """Set the smart home mode of a system (requires myUplink Premium).
        Typical modes: Default, Normal, Away, Vacation, Home. Confirm with the
        user first."""
        sys_id = _resolve_system(system_id)
        result = client.request(
            "PUT",
            f"/v2/systems/{sys_id}/smart-home-mode",
            json={"smartHomeMode": mode},
        )
        return {"system_id": sys_id, "mode": mode, "api_response": result}
