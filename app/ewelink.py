"""eWeLink (Sonoff) cloud API client — official developer platform OAuth2.

Signature scheme per eWeLink v2 API: the OAuth page and the token
endpoints authenticate the app with base64(HMAC-SHA256(secret, msg));
API calls use the Bearer access token plus the X-CK-Appid header.
Tokens persist in the kv table and refresh automatically.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import storage
from .config import settings
from .isolarcloud import make_transport

log = logging.getLogger("ewelink")

TOKENS_KEY = "ewelink_tokens"
OAUTH_PAGE = "https://c2ccdn.coolkit.cc/oauth/index.html"


def _api_base() -> str:
    return f"https://{settings.ewelink_region}-apia.coolkit.cc"


def enabled() -> bool:
    return bool(settings.ewelink_client_id and settings.ewelink_client_secret)


def connected() -> bool:
    return storage.kv_get(TOKENS_KEY) is not None


def _sign(message: bytes) -> str:
    return base64.b64encode(
        hmac.new(settings.ewelink_client_secret.encode(), message, hashlib.sha256).digest()
    ).decode()


def auth_url(redirect_uri: str, state: str) -> str:
    seq = str(int(time.time() * 1000))
    sig = _sign(f"{settings.ewelink_client_id}_{seq}".encode())
    return OAUTH_PAGE + "?" + urlencode({
        "state": state,
        "clientId": settings.ewelink_client_id,
        "authorization": sig,
        "seq": seq,
        "redirectUrl": redirect_uri,
        "nonce": pysecrets.token_hex(4),
        "grantType": "authorization_code",
        "showQRCode": "false",
    }, quote_via=quote)


def _save_tokens(data: dict[str, Any]) -> None:
    storage.kv_put(TOKENS_KEY, json.dumps({
        "access_token": data["accessToken"],
        "refresh_token": data.get("refreshToken"),
        # eWeLink access tokens last 30 days; refresh proactively earlier.
        "expires_at": time.time() + 25 * 86400,
    }))


async def _signed_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(body).encode()
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.post(f"{_api_base()}{path}", content=raw, headers={
            "Content-Type": "application/json",
            "X-CK-Appid": settings.ewelink_client_id,
            "X-CK-Nonce": pysecrets.token_hex(4),
            "Authorization": "Sign " + _sign(raw),
        })
        r.raise_for_status()
        data = r.json()
    if data.get("error"):
        raise RuntimeError(f"eWeLink error {data.get('error')}: {data.get('msg')}")
    return data.get("data", {})


async def exchange_code(code: str, redirect_uri: str) -> None:
    data = await _signed_post("/v2/user/oauth/token", {
        "code": code,
        "redirectUrl": redirect_uri,
        "grantType": "authorization_code",
    })
    _save_tokens(data)
    log.info("ewelink: account connected")


async def _access_token() -> str:
    raw = storage.kv_get(TOKENS_KEY)
    if raw is None:
        raise RuntimeError("eWeLink account not connected")
    tokens = json.loads(raw)
    if time.time() < tokens["expires_at"]:
        return tokens["access_token"]
    data = await _signed_post("/v2/user/refresh", {"rt": tokens["refresh_token"]})
    _save_tokens({
        "accessToken": data.get("at") or data.get("accessToken"),
        "refreshToken": data.get("rt") or data.get("refreshToken"),
    })
    return json.loads(storage.kv_get(TOKENS_KEY))["access_token"]


async def _request(method: str, path: str, **kwargs) -> dict[str, Any]:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20, transport=make_transport()) as http:
        r = await http.request(method, f"{_api_base()}{path}", headers={
            "Authorization": f"Bearer {token}",
            "X-CK-Appid": settings.ewelink_client_id,
            "X-CK-Nonce": pysecrets.token_hex(4),
            "Content-Type": "application/json",
        }, **kwargs)
        r.raise_for_status()
        data = r.json()
    if data.get("error"):
        raise RuntimeError(f"eWeLink error {data.get('error')}: {data.get('msg')}")
    return data.get("data", {})


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _power_w(params: dict[str, Any], uiid: int | None) -> float | None:
    p = _num(params.get("power"))
    if p is None:
        return None
    # Newer meters (POWR3/Elite/SPM, uiid 190/182/…) report hundredths of W.
    if isinstance(params.get("power"), int) and uiid in (182, 190, 5):
        return p / 100 if uiid in (182, 190) else p
    return p


async def get_devices() -> list[dict[str, Any]]:
    data = await _request("GET", "/v2/device/thing", params={"num": 0})
    out = []
    for item in data.get("thingList", []):
        d = item.get("itemData", {})
        params = d.get("params", {}) or {}
        uiid = (d.get("extra") or {}).get("uiid")
        switch = params.get("switch")
        if switch is None and isinstance(params.get("switches"), list):
            switch = params["switches"][0].get("switch") if params["switches"] else None
        out.append({
            "id": d.get("deviceid"),
            "name": d.get("name"),
            "model": d.get("productModel"),
            "online": bool(d.get("online")),
            "switch": switch,                       # "on" | "off" | None
            "multi_channel": isinstance(params.get("switches"), list),
            "power_w": _power_w(params, uiid),
            "voltage": _num(params.get("voltage")),
            "current": _num(params.get("current")),
        })
    return out


async def set_switch(device_id: str, on: bool, multi_channel: bool = False) -> None:
    value = "on" if on else "off"
    params: dict[str, Any] = (
        {"switches": [{"switch": value, "outlet": 0}]} if multi_channel
        else {"switch": value}
    )
    await _request("POST", "/v2/device/thing/status", json={
        "type": 1, "id": device_id, "params": params,
    })
    log.info("ewelink: %s -> %s", device_id, value)
