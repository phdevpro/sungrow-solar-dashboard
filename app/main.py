import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import settings
from .isolarcloud import ISolarCloudError, client

# Inverter/ESS production-power point per device type.
POWER_POINT = {14: "p13003", 1: "p24"}

_device_cache: dict[str, tuple[float, list[dict]]] = {}
_response_cache: dict[str, tuple[float, dict]] = {}


async def cached_devices(ps_id: str) -> list[dict]:
    now = time.time()
    hit = _device_cache.get(ps_id)
    if hit and now - hit[0] < 600:
        return hit[1]
    result = await client.get_device_list(ps_id)
    devices = result.get("pageList", [])
    _device_cache[ps_id] = (now, devices)
    return devices


def cache_get(key: str, ttl: float) -> dict | None:
    hit = _response_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def cache_put(key: str, value: dict) -> dict:
    _response_cache[key] = (time.time(), value)
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = settings.validate()
    if missing:
        raise RuntimeError(
            f"Missing configuration: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your credentials."
        )
    yield
    await client.close()


app = FastAPI(title="iSolarCloud Dashboard", lifespan=lifespan)

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


def _api_error(exc: ISolarCloudError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


@app.get("/api/plants")
async def plants():
    try:
        result = await client.get_power_station_list()
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return result


@app.get("/api/plants/{ps_id}/kpi")
async def plant_kpi(ps_id: str):
    try:
        result = await client.get_station_real_kpi([ps_id])
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return result


@app.get("/api/plants/{ps_id}/detail")
async def plant_detail(ps_id: str):
    try:
        result = await client.get_power_station_detail(ps_id)
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return result


@app.get("/api/plants/{ps_id}/devices")
async def plant_devices(ps_id: str):
    try:
        result = await client.get_device_list(ps_id)
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return result


@app.get("/api/plants/{ps_id}/curve")
async def plant_curve(ps_id: str, date: str | None = None):
    """5-minute production power curve (W) for the plant's inverter."""
    date = date or datetime.now().strftime("%Y%m%d")
    cached = cache_get(f"curve:{ps_id}:{date}", 270)
    if cached is not None:
        return cached
    try:
        devices = await cached_devices(ps_id)
        inverter = next(
            (d for d in devices if d.get("device_type") in POWER_POINT), None
        )
        if inverter is None:
            raise HTTPException(status_code=404, detail="No inverter found in plant")
        point = POWER_POINT[inverter["device_type"]]
        samples = await client.get_day_curve(inverter["ps_key"], point, date)
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return cache_put(f"curve:{ps_id}:{date}", {
        "date": date,
        "samples": [
            {"t": s["time_stamp"][8:12], "w": float(s[point])}
            for s in samples
            if s.get(point) not in (None, "", "--")
        ],
    })


@app.get("/api/plants/{ps_id}/panels")
async def plant_panels(ps_id: str):
    """Per-panel (optimizer) realtime output power and lifetime yield."""
    cached = cache_get(f"panels:{ps_id}", 55)
    if cached is not None:
        return cached
    try:
        optimizers = await client.get_optimizer_list(ps_id)
        if not optimizers:
            return {"panels": []}
        data = await client.get_optimizer_realtime(
            [o["ps_key"] for o in optimizers], ["58107", "58101"]
        )
    except ISolarCloudError as exc:
        raise _api_error(exc)
    sn_by_key = {o["ps_key"]: o.get("sn") for o in optimizers}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    panels = [
        {
            "ps_key": p.get("ps_key"),
            "sn": sn_by_key.get(p.get("ps_key")),
            "power_w": num(p.get("p58107")),
            "total_wh": num(p.get("p58101")),
            "time": p.get("device_time"),
            # device_time is null when the optimizer has never reported.
            "reporting": p.get("device_time") is not None,
        }
        for p in data
    ]
    panels.sort(key=lambda p: p["ps_key"] or "")
    return cache_put(f"panels:{ps_id}", {"panels": panels})


@app.get("/api/plants/{ps_id}/batteries")
async def plant_batteries(ps_id: str):
    """Per-battery SOC/SOH/temperature plus system-level SOC from the ESS."""
    cached = cache_get(f"batteries:{ps_id}", 55)
    if cached is not None:
        return cached
    try:
        devices = await cached_devices(ps_id)
        batteries = [d for d in devices if d.get("device_type") == 43]
        ess = next((d for d in devices if d.get("device_type") == 14), None)

        result: dict = {"batteries": [], "system": None}
        if batteries:
            data = await client.get_device_realtime_data(
                [b["ps_key"] for b in batteries],
                ["58601", "58602", "58603", "58604", "58605"],
                device_type=43,
            )
            points = [x["device_point"] for x in data.get("device_point_list", [])]

            def num(p, k):
                try:
                    return float(p[k])
                except (KeyError, TypeError, ValueError):
                    return None

            result["batteries"] = sorted(
                (
                    {
                        "name": p.get("device_name"),
                        "sn": p.get("device_sn"),
                        "soc": num(p, "p58604"),
                        "soh": num(p, "p58605"),
                        "temp_c": num(p, "p58603"),
                        "voltage": num(p, "p58601"),
                        "current": num(p, "p58602"),
                        "online": p.get("dev_status") == 1,
                    }
                    for p in points
                ),
                key=lambda b: b["name"] or "",
            )
        if ess:
            data = await client.get_device_realtime_data(
                [ess["ps_key"]], ["13141", "13142"], device_type=14
            )
            points = [x["device_point"] for x in data.get("device_point_list", [])]
            if points:
                p = points[0]
                try:
                    result["system"] = {
                        "soc": float(p.get("p13141")),
                        "soh": float(p.get("p13142")),
                    }
                except (TypeError, ValueError):
                    pass
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return cache_put(f"batteries:{ps_id}", result)
