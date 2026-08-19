"""Background collector: polls the iSolarCloud API on an interval and
persists samples to SQLite, so historical queries never hit the API."""

import asyncio
import logging
import time
from datetime import datetime

from . import storage
from .isolarcloud import ISolarCloudError, client

log = logging.getLogger("collector")

INTERVAL = 300  # seconds; the cloud updates device data every 5 minutes

# Inverter/ESS production-power point per device type.
POWER_POINT = {14: "p13003", 1: "p24"}


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


async def collect_once() -> None:
    plants = (await client.get_power_station_list()).get("pageList", [])
    for plant in plants:
        await _collect_plant(plant)


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
