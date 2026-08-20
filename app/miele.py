"""Miele 3rd Party API client (Miele@home appliances).

OAuth2 authorization-code flow against api.mcs3.miele.com; tokens are
persisted in the kv table so they survive restarts, and refreshed
automatically. Remote start needs the appliance's SmartStart/remote
start enabled — the API then allows processAction 1 (start) while the
appliance sits in "waiting to start".
"""

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import storage
from .config import settings
from .isolarcloud import make_transport

log = logging.getLogger("miele")

BASE = "https://api.mcs3.miele.com"
TOKENS_KEY = "miele_tokens"

STATUS = {
    1: "Off", 2: "Standby", 3: "Programmed", 4: "Ready to start",
    5: "Running", 6: "Paused", 7: "Finished", 8: "Failure",
    9: "Interrupted", 10: "Idle", 11: "Rinse hold", 12: "Service",
    145: "Locked",
}

PROCESS_START = 1
PROCESS_STOP = 2


def enabled() -> bool:
    return bool(settings.miele_client_id and settings.miele_client_secret)


def connected() -> bool:
    return storage.kv_get(TOKENS_KEY) is not None


def auth_url(redirect_uri: str, state: str) -> str:
    return f"{BASE}/thirdparty/login?" + urlencode({
        "client_id": settings.miele_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })


def _save_tokens(data: dict[str, Any]) -> None:
    storage.kv_put(TOKENS_KEY, json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 3600) - 300,
    }))


async def exchange_code(code: str, redirect_uri: str) -> None:
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.post(f"{BASE}/thirdparty/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.miele_client_id,
            "client_secret": settings.miele_client_secret,
        })
        r.raise_for_status()
        _save_tokens(r.json())
    log.info("miele: account connected")


async def _access_token() -> str:
    raw = storage.kv_get(TOKENS_KEY)
    if raw is None:
        raise RuntimeError("Miele account not connected")
    tokens = json.loads(raw)
    if time.time() < tokens["expires_at"]:
        return tokens["access_token"]
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.post(f"{BASE}/thirdparty/token", data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": settings.miele_client_id,
            "client_secret": settings.miele_client_secret,
        })
        r.raise_for_status()
        _save_tokens(r.json())
        return r.json()["access_token"]


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        return await http.request(
            method, f"{BASE}/v1{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs,
        )


async def get_devices() -> list[dict[str, Any]]:
    r = await _request("GET", "/devices")
    r.raise_for_status()
    out = []
    for dev_id, d in r.json().items():
        state = d.get("state", {})
        ident = d.get("ident", {})
        remote = state.get("remoteEnable", {})
        remaining = state.get("remainingTime") or [0, 0]
        status_code = state.get("status", {}).get("value_raw")
        out.append({
            "id": dev_id,
            "name": ident.get("deviceName")
                or ident.get("type", {}).get("value_localized")
                or f"Device {dev_id}",
            "type": ident.get("type", {}).get("value_localized"),
            "status_code": status_code,
            "status": state.get("status", {}).get("value_localized")
                or STATUS.get(status_code, "Unknown"),
            "program": state.get("ProgramID", {}).get("value_localized"),
            "phase": state.get("programPhase", {}).get("value_localized"),
            "remaining_min": remaining[0] * 60 + remaining[1],
            "remote_start": bool(remote.get("fullRemoteControl")),
            # Startable: programmed and waiting for (remote) start.
            "startable": status_code in (3, 4) and bool(remote.get("fullRemoteControl")),
        })
    return out


async def start(device_id: str) -> None:
    r = await _request(
        "PUT", f"/devices/{device_id}/actions",
        json={"processAction": PROCESS_START},
    )
    r.raise_for_status()
    log.info("miele: start sent to %s", device_id)
