"""Samsung SmartThings client — air conditioners (WindFree and similar).

OAuth2 authorization-code flow with an "OAuth-In" app created via the
SmartThings CLI (`smartthings apps:create`), scopes r:devices:* and
x:devices:*. Access tokens last 24h; refresh tokens rotate on every
refresh, so the new pair is persisted each time in the kv table.
"""

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import storage
from .config import settings
from .isolarcloud import make_transport

log = logging.getLogger("smartthings")

API = "https://api.smartthings.com/v1"
AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://auth-global.api.smartthings.com/oauth/token"
SCOPES = "r:devices:* x:devices:*"
TOKENS_KEY = "smartthings_tokens"


def enabled() -> bool:
    return bool(settings.smartthings_client_id and settings.smartthings_client_secret)


def connected() -> bool:
    return storage.kv_get(TOKENS_KEY) is not None


def disconnect() -> None:
    """Forget the tokens so the UI offers to reconnect the account."""
    storage.kv_delete(TOKENS_KEY)
    log.warning("smartthings: tokens rejected, account disconnected")


def auth_url(redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": settings.smartthings_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })


def _save_tokens(data: dict[str, Any]) -> None:
    storage.kv_put(TOKENS_KEY, json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 86400) - 600,
    }))


async def _token_request(form: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.post(
            TOKEN_URL, data={**form, "client_id": settings.smartthings_client_id},
            auth=(settings.smartthings_client_id, settings.smartthings_client_secret),
        )
        r.raise_for_status()
        return r.json()


async def exchange_code(code: str, redirect_uri: str) -> None:
    _save_tokens(await _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }))
    log.info("smartthings: account connected")


# Refresh tokens rotate: two concurrent refreshes would burn the old one
# and leave one caller with a revoked token.
_refresh_lock = asyncio.Lock()


async def _access_token() -> str:
    async with _refresh_lock:
        raw = storage.kv_get(TOKENS_KEY)
        if raw is None:
            raise RuntimeError("SmartThings account not connected")
        tokens = json.loads(raw)
        if time.time() < tokens["expires_at"]:
            return tokens["access_token"]
        try:
            data = await _token_request({
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            })
        except httpx.HTTPStatusError as exc:
            # Refresh token expired (30 days unused) or revoked.
            if exc.response.status_code in (400, 401):
                disconnect()
            raise
        _save_tokens(data)
        return data["access_token"]


async def _request(method: str, path: str, **kwargs) -> Any:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.request(
            method, f"{API}{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs,
        )
        if r.status_code == 401:
            disconnect()
        r.raise_for_status()
        return r.json() if r.content else {}


def _attr(main: dict[str, Any], capability: str, attribute: str) -> Any:
    return ((main.get(capability) or {}).get(attribute) or {}).get("value")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _device_state(dev: dict[str, Any]) -> dict[str, Any]:
    dev_id = dev["deviceId"]
    status, health = await asyncio.gather(
        _request("GET", f"/devices/{dev_id}/status"),
        _request("GET", f"/devices/{dev_id}/health"),
    )
    main = (status.get("components") or {}).get("main", {})
    report = _attr(main, "powerConsumptionReport", "powerConsumption") or {}
    return {
        "id": dev_id,
        "name": dev.get("label") or dev.get("name") or dev_id,
        "online": health.get("state") == "ONLINE",
        "switch": _attr(main, "switch", "switch"),           # "on" | "off"
        "mode": _attr(main, "airConditionerMode", "airConditionerMode"),
        "modes": _attr(main, "airConditionerMode", "supportedAcModes") or [],
        "fan_mode": _attr(main, "airConditionerFanMode", "fanMode"),
        "setpoint_c": _num(_attr(main, "thermostatCoolingSetpoint", "coolingSetpoint")),
        "temp_c": _num(_attr(main, "temperatureMeasurement", "temperature")),
        "humidity": _num(_attr(main, "relativeHumidityMeasurement", "humidity")),
        # powerConsumptionReport refreshes roughly every 10 minutes.
        "power_w": _num(report.get("power")),
        "energy_wh": _num(report.get("energy")),
    }


# Samsung RACs don't push every change to the cloud (e.g. switching off
# with the IR remote), so SmartThings can keep serving a stale "on" for
# hours. A `refresh` command makes the unit re-report its real state a
# few seconds later; throttled so polling stays within command limits.
REFRESH_EVERY = 90      # seconds between refreshes of the same unit
REFRESH_SETTLE = 5      # seconds the unit needs to report back
_last_refresh: dict[str, float] = {}
_background: set[asyncio.Task] = set()


async def _refresh(ids: list[str]) -> None:
    async def one(dev_id: str) -> None:
        _last_refresh[dev_id] = time.time()
        try:
            await _command(dev_id, "refresh", "refresh")
        except Exception as exc:
            log.warning("smartthings: refresh %s failed: %s", dev_id, exc)
    await asyncio.gather(*(one(i) for i in ids))


async def get_devices(fresh: bool = False) -> list[dict[str, Any]]:
    """Air conditioners on the account with their current state.

    fresh=True (automation decisions): refresh units not refreshed
    recently and wait for them to report before reading. Otherwise the
    refresh runs in the background and the next read picks it up.
    """
    data = await _request("GET", "/devices", params={"capability": "airConditionerMode"})
    items = data.get("items", [])
    due = [d["deviceId"] for d in items
           if time.time() - _last_refresh.get(d["deviceId"], 0) > REFRESH_EVERY]
    if due:
        if fresh:
            await _refresh(due)
            await asyncio.sleep(REFRESH_SETTLE)
        else:
            task = asyncio.create_task(_refresh(due))
            _background.add(task)
            task.add_done_callback(_background.discard)
    return list(await asyncio.gather(*(_device_state(d) for d in items)))


async def _command(device_id: str, capability: str, command: str, args: list | None = None) -> None:
    cmd: dict[str, Any] = {"component": "main", "capability": capability, "command": command}
    if args:
        cmd["arguments"] = args
    await _request("POST", f"/devices/{device_id}/commands", json={"commands": [cmd]})
    if capability != "refresh":
        log.info("smartthings: %s %s %s %s", device_id, capability, command, args or "")


async def set_switch(device_id: str, on: bool) -> None:
    await _command(device_id, "switch", "on" if on else "off")


async def set_mode(device_id: str, mode: str) -> None:
    await _command(device_id, "airConditionerMode", "setAirConditionerMode", [mode])


async def set_setpoint(device_id: str, celsius: float) -> None:
    # Samsung ACs use the cooling setpoint as the target in every mode.
    await _command(device_id, "thermostatCoolingSetpoint", "setCoolingSetpoint", [celsius])
