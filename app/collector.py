"""Background collector: polls the iSolarCloud API on an interval and
persists samples to SQLite, so historical queries never hit the API."""

import asyncio
import json
import logging
import time
from datetime import datetime

from . import ewelink, miele, smartthings, storage, wallconnector
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


# Solar-surplus air conditioning. Opt-in per unit, in priority order;
# only units the automation switched on are ever switched off by it, so
# manual use is never overridden. State lives in settings/kv.
ac_auto = {"enabled": False, "devices": [], "last_action_ts": 0.0, "deficit_cycles": 0}
AC_COOLDOWN = 600       # min seconds between AC actions (compressor protection)
AC_STOP_CYCLES = 2      # consecutive deficit cycles before switching off
AC_STARTED_KEY = "ac_auto_started"


def load_ac_auto() -> None:
    ac_auto["enabled"] = storage.get_setting("ac_auto") == "1"
    ac_auto["devices"] = list(json.loads(storage.get_setting("ac_auto_devices") or "[]"))


def set_ac_auto(enabled: bool | None = None, order: list[str] | None = None) -> None:
    """order: the opted-in unit ids, highest priority first."""
    # Turning automation off (globally or for a unit) hands those units
    # back to manual control as they are: nothing is switched off.
    if enabled is not None:
        ac_auto["enabled"] = enabled
        storage.set_setting("ac_auto", "1" if enabled else "0")
        if not enabled:
            _save_ac_started(set())
    if order is not None:
        order = list(dict.fromkeys(str(i) for i in order))
        _save_ac_started(ac_auto_started() & set(order))
        ac_auto["devices"] = order
        storage.set_setting("ac_auto_devices", json.dumps(order))


def ac_auto_started() -> set[str]:
    return set(json.loads(storage.kv_get(AC_STARTED_KEY) or "[]"))


def _save_ac_started(ids: set[str]) -> None:
    storage.kv_put(AC_STARTED_KEY, json.dumps(sorted(ids)))

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
                # Appliances first (a finite program), then the ACs; the
                # flow reading is stale after a start, so no AC start
                # in the same cycle.
                miele_started = await _miele_auto_start(flow)
                await _ac_auto(flow, allow_start=not miele_started)
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


BATT_FULL_SOC = 0.97    # above this the battery tapers: no charge headroom
EXPORT_IDLE_W = 100     # export below this = nothing left over


def batt_max_charge_w() -> float:
    if settings.batt_max_charge_w:
        return settings.batt_max_charge_w
    volts = storage.latest_battery_voltage()
    return settings.batt_max_charge_a * (volts or 105.0)


def surplus(flow: dict) -> dict:
    """Where the PV power that loads could use is going right now.

    export: sold to the grid — real surplus, because the battery only lets
    power through to the grid once it is full or charging at its limit.
    deficit: power a load is taking that should not go to it: grid import,
    battery discharge, and charge headroom (the battery could absorb more).
    """
    export = flow.get("grid_export_w") or 0
    chg = flow.get("batt_charge_w") or 0
    soc = flow.get("soc")
    headroom = 0.0
    if soc is not None and soc < BATT_FULL_SOC:
        headroom = max(0.0, batt_max_charge_w() - chg)
    deficit = (flow.get("grid_import_w") or 0) + (flow.get("batt_discharge_w") or 0) + headroom
    return {"export": export, "deficit": deficit, "headroom": headroom, "soc": soc}


async def _miele_auto_start(flow: dict) -> bool:
    """Start one appliance waiting for remote start when the grid export
    exceeds the surplus threshold. One start per cooldown window.
    Returns True when an appliance was started."""
    if not (miele_auto["enabled"] and miele.enabled() and miele.connected()):
        return False
    if time.time() - miele_auto["last_start_ts"] < MIELE_COOLDOWN:
        return False
    if (flow.get("grid_export_w") or 0) < settings.miele_auto_surplus_w:
        return False
    soc = flow.get("soc")
    if settings.miele_auto_min_soc and (soc is None or soc < settings.miele_auto_min_soc):
        return False
    try:
        devices = await miele.get_devices()
        candidate = next((d for d in devices if d["startable"]), None)
        if candidate is None:
            return False
        await miele.start(candidate["id"])
        miele_auto["last_start_ts"] = time.time()
        log.info(
            "miele auto-start: %s (%s) at export %.0f W",
            candidate["name"], candidate["type"], flow.get("grid_export_w") or 0,
        )
        return True
    except Exception as exc:
        log.warning("miele auto-start failed: %s", exc)
        return False


async def _ac_auto(flow: dict, allow_start: bool = True) -> None:
    """Use exported power for air conditioning, one unit at a time.

    Start: export above the threshold → switch on the highest-priority
    opted-in unit that is off. Stop: a unit the automation started now
    takes power the battery could absorb or that comes from grid/battery
    (deficit) for AC_STOP_CYCLES cycles → switch off the lowest-priority
    started unit. One action per AC_COOLDOWN.
    """
    if not (smartthings.enabled() and smartthings.connected()):
        return
    if not (ac_auto["enabled"] and ac_auto["devices"]):
        return
    order = ac_auto["devices"]
    started = ac_auto_started()
    sp = surplus(flow)
    stealing = sp["export"] < EXPORT_IDLE_W and sp["deficit"] > settings.ac_auto_stop_w
    ac_auto["deficit_cycles"] = ac_auto["deficit_cycles"] + 1 if stealing else 0
    try:
        devices = {d["id"]: d for d in await smartthings.get_devices(fresh=True)}
        # Forget units that are off now (switched off by hand or by us).
        still = {i for i in started if devices.get(i, {}).get("switch") == "on"}
        if still != started:
            started = still
            _save_ac_started(started)
        if time.time() - ac_auto["last_action_ts"] < AC_COOLDOWN:
            return

        soc = sp["soc"]
        low_soc = bool(settings.ac_auto_min_soc) and (soc is None or soc < settings.ac_auto_min_soc)
        if started and (ac_auto["deficit_cycles"] >= AC_STOP_CYCLES or low_soc):
            rank = {i: n for n, i in enumerate(order)}
            dev_id = max(started, key=lambda i: rank.get(i, len(order)))
            await smartthings.set_switch(dev_id, False)
            started.discard(dev_id)
            _save_ac_started(started)
            ac_auto["last_action_ts"] = time.time()
            ac_auto["deficit_cycles"] = 0
            log.info("ac auto-stop: %s (deficit %.0f W, headroom %.0f W, soc %s)",
                     devices.get(dev_id, {}).get("name", dev_id), sp["deficit"], sp["headroom"], soc)
            return

        if not allow_start or low_soc or sp["export"] < settings.ac_auto_surplus_w:
            return
        candidate = next((devices[i] for i in order
                          if i in devices and devices[i]["online"] and devices[i]["switch"] == "off"),
                         None)
        if candidate is None:
            return
        await smartthings.set_switch(candidate["id"], True)
        started.add(candidate["id"])
        _save_ac_started(started)
        ac_auto["last_action_ts"] = time.time()
        ac_auto["deficit_cycles"] = 0
        log.info("ac auto-start: %s (priority %d, %s) at export %.0f W",
                 candidate["name"], order.index(candidate["id"]) + 1, candidate["mode"], sp["export"])
    except Exception as exc:
        log.warning("ac auto failed: %s", exc)


async def collect_once() -> None:
    plants = (await client.get_power_station_list()).get("pageList", [])
    for plant in plants:
        await _collect_plant(plant)

    # Sonoff snapshot: power meters (POW) and temperature probes (TH/THR).
    if ewelink.enabled() and ewelink.connected():
        try:
            devices = await ewelink.get_devices()
            storage.store_sonoff(
                time.strftime("%Y%m%d%H%M%S"),
                [d for d in devices
                 if d.get("power_w") is not None or d.get("temp_c") is not None],
            )
        except Exception as exc:
            log.warning("sonoff snapshot failed: %s", exc)

    # SmartThings air conditioners.
    if smartthings.enabled() and smartthings.connected():
        try:
            storage.store_aircon(time.strftime("%Y%m%d%H%M%S"),
                                 await smartthings.get_devices(fresh=True))
        except Exception as exc:
            log.warning("aircon snapshot failed: %s", exc)

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
