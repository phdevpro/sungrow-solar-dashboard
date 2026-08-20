"""Background collector: polls the iSolarCloud API on an interval and
persists samples to SQLite, so historical queries never hit the API."""

import asyncio
import logging
import time
from datetime import datetime

from . import miele, storage, wallconnector
from .config import settings
from .isolarcloud import ISolarCloudError, client

# Solar-surplus appliance auto-start. Persisted in the kv table so the
# UI toggle survives restarts; the MIELE_AUTO env only seeds first boot.
miele_auto = {"enabled": settings.miele_auto, "last_start_ts": 0.0}
MIELE_COOLDOWN = 900  # don't start more than one appliance per 15 min


def load_miele_auto() -> None:
    saved = storage.get_setting("miele_auto")
    if saved is not None:
        miele_auto["enabled"] = saved == "1"


def set_miele_auto(enabled: bool) -> None:
    miele_auto["enabled"] = enabled
    storage.set_setting("miele_auto", "1" if enabled else "0")

log = logging.getLogger("collector")

INTERVAL = 300  # seconds; the cloud updates device data every 5 minutes

# Inverter/ESS production-power point per device type.
POWER_POINT = {14: "p13003", 1: "p24"}

# ESS (type 14) energy-flow points, per the official measuring-point table:
# 13003 total DC (PV) W, 13011 active power W, 13119 load W, 13121 feed-in W,
# 13149 purchased W, 13126 battery charging W, 13150 battery discharging W,
# 13141 SOC; daily energies: 13112 PV, 13199 load, 13122 export, 13147 import,
# 13028 battery charge, 13029 battery discharge.
FLOW_POINTS = [
    "13003", "13011", "13119", "13121", "13149", "13126", "13150", "13141",
    "13112", "13199", "13122", "13147", "13028", "13029",
]


async def fetch_flow(ess_ps_key: str) -> dict | None:
    """Realtime energy flow snapshot from the ESS inverter."""
    data = await client.get_device_realtime_data([ess_ps_key], FLOW_POINTS, device_type=14)
    points = [x["device_point"] for x in data.get("device_point_list", [])]
    if not points:
        return None
    p = points[0]
    g = lambda k: _f(p.get(k))
    imp, exp = g("p13149") or 0.0, g("p13121") or 0.0
    chg, dis = g("p13126") or 0.0, g("p13150") or 0.0
    return {
        "pv_w": g("p13003"),
        "ac_w": g("p13011"),
        "load_w": g("p13119"),
        "grid_import_w": imp,
        "grid_export_w": exp,
        "grid_w": imp - exp,          # + import / - export
        "batt_charge_w": chg,
        "batt_discharge_w": dis,
        "batt_w": chg - dis,          # + charging / - discharging
        "soc": g("p13141"),
        "time": p.get("device_time"),
        "today": {
            "pv_wh": g("p13112"),
            "load_wh": g("p13199"),
            "export_wh": g("p13122"),
            "import_wh": g("p13147"),
            "batt_charge_wh": g("p13028"),
            "batt_discharge_wh": g("p13029"),
        },
    }


async def _collect_plant(plant: dict) -> None:
    ps_id = str(plant["ps_id"])
    now = time.strftime("%Y%m%d%H%M%S")

    def num(v):
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    storage.store_kpi(
        ps_id, now,
        (num(plant.get("curr_power")) or 0) * 1000,   # kW -> W
        num(plant.get("today_energy")),
        num(plant.get("total_energy")),
    )

    devices = (await client.get_device_list(ps_id)).get("pageList", [])

    # Inverter power curve: pull the last 2h window and upsert (idempotent).
    inverter = next((d for d in devices if d.get("device_type") in POWER_POINT), None)
    if inverter:
        point = POWER_POINT[inverter["device_type"]]
        date = datetime.now().strftime("%Y%m%d")
        hour = datetime.now().hour
        start = f"{date}{max(hour - 1, 0):02d}0000"
        end = f"{date}{hour:02d}5959"
        try:
            r = await client.get_device_minute_data([inverter["ps_key"]], point, start, end)
            samples = [
                (row["time_stamp"], float(row[point]))
                for row in r.get(inverter["ps_key"], [])
                if row.get(point) not in (None, "", "--")
            ]
            storage.store_power(ps_id, samples)
        except ISolarCloudError as exc:
            log.warning("power curve failed for %s: %s", ps_id, exc)

    # Optimizer (per-panel) snapshot.
    try:
        optimizers = await client.get_optimizer_list(ps_id)
        if optimizers:
            data = await client.get_optimizer_realtime(
                [o["ps_key"] for o in optimizers], ["58107", "58101"]
            )
            storage.store_panels([
                {
                    "ps_key": p.get("ps_key"),
                    "time": p.get("device_time"),
                    "power_w": _f(p.get("p58107")),
                    "total_wh": _f(p.get("p58101")),
                }
                for p in data
            ])
    except ISolarCloudError as exc:
        log.warning("optimizer snapshot failed for %s: %s", ps_id, exc)

    # Energy-flow snapshot (PV / load / grid / battery).
    if inverter and inverter.get("device_type") == 14:
        try:
            flow = await fetch_flow(inverter["ps_key"])
            if flow:
                storage.store_flow(ps_id, flow.get("time") or now, flow)
                await _miele_auto_start(flow)
        except ISolarCloudError as exc:
            log.warning("flow snapshot failed for %s: %s", ps_id, exc)

    # Battery snapshot.
    batteries = [d for d in devices if d.get("device_type") == 43]
    if batteries:
        try:
            data = await client.get_device_realtime_data(
                [b["ps_key"] for b in batteries],
                ["58601", "58602", "58603", "58604", "58605"],
                device_type=43,
            )
            points = [x["device_point"] for x in data.get("device_point_list", [])]
            ts = points[0].get("device_time") or now if points else now
            storage.store_batteries(ts, [
                {
                    "sn": p.get("device_sn"),
                    "soc": _f(p.get("p58604")),
                    "soh": _f(p.get("p58605")),
                    "temp_c": _f(p.get("p58603")),
                    "voltage": _f(p.get("p58601")),
                    "current": _f(p.get("p58602")),
                }
                for p in points
            ])
        except ISolarCloudError as exc:
            log.warning("battery snapshot failed for %s: %s", ps_id, exc)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _miele_auto_start(flow: dict) -> None:
    """Start one appliance waiting for remote start when the grid export
    exceeds the surplus threshold. One start per cooldown window."""
    if not (miele_auto["enabled"] and miele.enabled() and miele.connected()):
        return
    if time.time() - miele_auto["last_start_ts"] < MIELE_COOLDOWN:
        return
    if (flow.get("grid_export_w") or 0) < settings.miele_auto_surplus_w:
        return
    # Never drain a low house battery into an appliance start.
    soc = flow.get("soc")
    if soc is None or soc < settings.miele_auto_min_soc:
        return
    try:
        devices = await miele.get_devices()
        candidate = next((d for d in devices if d["startable"]), None)
        if candidate is None:
            return
        await miele.start(candidate["id"])
        miele_auto["last_start_ts"] = time.time()
        log.info(
            "miele auto-start: %s (%s) at export %.0f W",
            candidate["name"], candidate["type"], flow.get("grid_export_w") or 0,
        )
    except Exception as exc:
        log.warning("miele auto-start failed: %s", exc)


async def collect_once() -> None:
    plants = (await client.get_power_station_list()).get("pageList", [])
    for plant in plants:
        await _collect_plant(plant)

    # Wall Connector on the same LAN (remote deployments use the push
    # agent + /api/ev/ingest instead).
    if wallconnector.enabled():
        try:
            s = await wallconnector.fetch_status()
            storage.store_ev(time.strftime("%Y%m%d%H%M%S"), s)
        except Exception as exc:
            log.warning("wall connector poll failed: %s", exc)


async def run_forever() -> None:
    """Poll loop; started from the FastAPI lifespan."""
    while True:
        try:
            await collect_once()
            log.info("collect cycle done")
        except ISolarCloudError as exc:
            log.warning("collect cycle failed: %s", exc)
        except Exception:
            log.exception("collect cycle crashed")
        await asyncio.sleep(INTERVAL)
