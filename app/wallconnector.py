"""Tesla Wall Connector Gen 3 local API client.

The Gen 3 unit exposes an unauthenticated HTTP API on its LAN address:
/api/1/vitals (live state) and /api/1/lifetime (counters). Enabled when
TWC_HOST is set and the host is reachable from this process.
"""

from typing import Any

import httpx

from .config import settings


def enabled() -> bool:
    return bool(settings.twc_host)


async def _get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as http:
        r = await http.get(f"http://{settings.twc_host}{path}")
        r.raise_for_status()
        return r.json()


async def fetch_status() -> dict[str, Any]:
    """Live charging state; power is derived from grid voltage × current."""
    v = await _get("/api/1/vitals")
    volts = v.get("grid_v") or 0
    amps = v.get("vehicle_current_a") or 0
    return {
        "vehicle_connected": bool(v.get("vehicle_connected")),
        "charging": bool(v.get("contactor_closed")),
        "power_w": round(volts * amps, 1),
        "voltage": volts,
        "current_a": amps,
        "session_wh": v.get("session_energy_wh"),
        "session_s": v.get("session_s"),
        "handle_temp_c": v.get("handle_temp_c"),
        "pcba_temp_c": v.get("pcba_temp_c"),
    }


async def fetch_lifetime() -> dict[str, Any]:
    lt = await _get("/api/1/lifetime")
    return {
        "energy_wh": lt.get("energy_wh"),
        "charge_starts": lt.get("charge_starts"),
        "uptime_s": lt.get("uptime_s"),
    }
