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


# Solar-surplus air conditioning. Opt-in per unit; only units the
# automation switched on are ever switched off by it, so manual use is
# never overridden. State lives in settings/kv to survive restarts.
ac_auto = {"enabled": False, "devices": set(), "last_action_ts": 0.0, "deficit_cycles": 0}
AC_COOLDOWN = 600       # min seconds between AC actions (compressor protection)
AC_STOP_CYCLES = 2      # consecutive deficit cycles before switching off
AC_STARTED_KEY = "ac_auto_started"


def load_ac_auto() -> None:
    ac_auto["enabled"] = storage.get_setting("ac_auto") == "1"
    ac_auto["devices"] = set(json.loads(storage.get_setting("ac_auto_devices") or "[]"))


def set_ac_auto(enabled: bool | None = None, device_id: str | None = None,
                device_on: bool | None = None) -> None:
    # Turning automation off (globally or for a unit) hands those units
    # back to manual control as they are: nothing is switched off.
    if enabled is not None:
        ac_auto["enabled"] = enabled
        storage.set_setting("ac_auto", "1" if enabled else "0")
        if not enabled:
            _save_ac_started(set())
    if device_id is not None:
        if device_on:
            ac_auto["devices"].add(device_id)
        else:
            ac_auto["devices"].discard(device_id)
            _save_ac_started(ac_auto_started() - {device_id})
        storage.set_setting("ac_auto_devices", json.dumps(sorted(ac_auto["devices"])))


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
                await _miele_auto_start(flow)
                await _ac_auto(flow)
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


async def _ac_auto(flow: dict) -> None:
    """Switch on one opted-in AC on solar surplus; switch off the units
    it started when the house runs on battery/grid for a sustained time."""
    if not (smartthings.enabled() and smartthings.connected()):
        return
    if not (ac_auto["enabled"] and ac_auto["devices"]):
        return
    started = ac_auto_started()
    export = flow.get("grid_export_w") or 0
    deficit = (flow.get("grid_import_w") or 0) + (flow.get("batt_discharge_w") or 0)
    soc = flow.get("soc")
    ac_auto["deficit_cycles"] = ac_auto["deficit_cycles"] + 1 if deficit > settings.ac_auto_stop_w else 0
    try:
        devices = {d["id"]: d for d in await smartthings.get_devices()}
        # Forget units that are off now (switched off by hand or by us),
        # or that the user took out of automation.
        still = {i for i in started if devices.get(i, {}).get("switch") == "on"}
        if still != started:
            started = still
            _save_ac_started(started)
        if time.time() - ac_auto["last_action_ts"] < AC_COOLDOWN:
            return

        low_soc = soc is not None and soc < settings.ac_auto_min_soc
        if started and (ac_auto["deficit_cycles"] >= AC_STOP_CYCLES or low_soc):
            dev_id = sorted(started)[0]
            await smartthings.set_switch(dev_id, False)
            started.discard(dev_id)
            _save_ac_started(started)
            ac_auto["last_action_ts"] = time.time()
            log.info("ac auto-stop: %s (deficit %.0f W, soc %s)",
                     devices[dev_id]["name"], deficit, soc)
            return

        if export < settings.ac_auto_surplus_w or soc is None or low_soc:
            return
        candidate = next((d for i, d in devices.items()
                          if i in ac_auto["devices"] and d["online"] and d["switch"] == "off"),
                         None)
        if candidate is None:
            return
        await smartthings.set_switch(candidate["id"], True)
        started.add(candidate["id"])
        _save_ac_started(started)
        ac_auto["last_action_ts"] = time.time()
        ac_auto["deficit_cycles"] = 0
        log.info("ac auto-start: %s (%s) at export %.0f W",
                 candidate["name"], candidate["mode"], export)
    except Exception as exc:
        log.warning("ac auto failed: %s", exc)


async def collect_once() -> None:
    plants = (await client.get_power_station_list()).get("pageList", [])
    for plant in plants:
        await _collect_plant(plant)

    # Sonoff power-metering snapshot (POW models).
    if ewelink.enabled() and ewelink.connected():
        try:
            devices = await ewelink.get_devices()
            storage.store_sonoff(
                time.strftime("%Y%m%d%H%M%S"),
                [d for d in devices if d.get("power_w") is not None],
            )
        except Exception as exc:
            log.warning("sonoff snapshot failed: %s", exc)

    # SmartThings air conditioners.
    if smartthings.enabled() and smartthings.connected():
        try:
            storage.store_aircon(time.strftime("%Y%m%d%H%M%S"),
                                 await smartthings.get_devices())
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
