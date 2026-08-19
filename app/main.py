import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth, collector, storage, wallconnector
from .collector import POWER_POINT, fetch_flow
from .config import settings
from .isolarcloud import ISolarCloudError, client, make_transport

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
    storage.connect()
    task = asyncio.create_task(collector.run_forever())
    yield
    task.cancel()
    await client.close()
    storage.close()


app = FastAPI(title="Sungrow Solar Dashboard", lifespan=lifespan)

STATIC = Path(__file__).parent / "static"
INDEX_HTML = (STATIC / "index.html").read_text()
LOGIN_HTML = (STATIC / "login.html").read_text()

# Reachable without a session: login page, the PWA shell, and the EV
# ingest endpoint (which authenticates with its own bearer token).
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/api/ev/ingest"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    if auth.enabled():
        path = request.url.path
        if path not in PUBLIC_PATHS and not path.startswith("/icons/"):
            if not auth.check_token(request.cookies.get(auth.SESSION_COOKIE, "")):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def _is_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return LOGIN_HTML.replace("<!--ERROR-->", "")


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if auth.enabled() and auth.check_credentials(username, password):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            auth.SESSION_COOKIE,
            auth.make_token(),
            max_age=auth.SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
        )
        return resp
    await asyncio.sleep(0.5)  # soften brute-force attempts
    return HTMLResponse(
        LOGIN_HTML.replace("<!--ERROR-->", '<div class="err">Wrong username or password.</div>'),
        status_code=401,
    )


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript")


@app.get("/icons/{name}")
async def icon(name: str):
    path = STATIC / "icons" / name
    if not path.is_file() or path.parent != STATIC / "icons":
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


def _api_error(exc: ISolarCloudError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


@app.exception_handler(httpx.HTTPError)
async def httpx_error_handler(request: Request, exc: httpx.HTTPError):
    # Network/HTTP failures talking to the upstream gateway (timeouts, DNS,
    # 4xx/5xx) — surface the reason instead of a bare 500.
    return JSONResponse(
        {"detail": f"Upstream error: {type(exc).__name__}: {exc}"}, status_code=502
    )


@app.get("/api/plants")
async def plants():
    cached = cache_get("plants", 55)
    if cached is not None:
        return cached
    try:
        result = await client.get_power_station_list()
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return cache_put("plants", result)


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
    cached = cache_get(f"devices:{ps_id}", 600)
    if cached is not None:
        return cached
    try:
        result = await client.get_device_list(ps_id)
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return cache_put(f"devices:{ps_id}", result)


@app.get("/api/plants/{ps_id}/curve")
async def plant_curve(ps_id: str, date: str | None = None):
    """5-minute production power curve (W) for the plant's inverter.

    Served from the local DB when it has good coverage for the day;
    otherwise backfilled from the API (and stored, so next time is local).
    """
    date = date or datetime.now().strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")

    # Expected 5-min samples for the requested day so far.
    minutes = (datetime.now().hour * 60 + datetime.now().minute) if date == today else 1440
    expected = minutes / 5
    if storage.power_day_count(ps_id, date) >= expected * 0.8:
        return {"date": date, "samples": storage.load_power_day(ps_id, date), "source": "db"}

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
    clean = [
        (s["time_stamp"], float(s[point]))
        for s in samples
        if s.get(point) not in (None, "", "--")
    ]
    storage.store_power(ps_id, clean)
    return cache_put(f"curve:{ps_id}:{date}", {
        "date": date,
        "samples": [{"t": ts[8:12], "w": w} for ts, w in clean],
        "source": "api",
    })


@app.get("/api/plants/{ps_id}/panels")
async def plant_panels(ps_id: str):
    """Per-panel (optimizer) realtime output power and lifetime yield."""
    cached = cache_get(f"panels:{ps_id}", 240)
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
    cached = cache_get(f"batteries:{ps_id}", 240)
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
                [ess["ps_key"]], ["13141", "13142", "13140"], device_type=14
            )
            points = [x["device_point"] for x in data.get("device_point_list", [])]
            if points:
                p = points[0]

                def numf(k):
                    try:
                        return float(p.get(k))
                    except (TypeError, ValueError):
                        return None

                if numf("p13141") is not None:
                    result["system"] = {
                        "soc": numf("p13141"),
                        "soh": numf("p13142"),
                        # p13140: total battery capacity in Wh
                        "capacity_wh": numf("p13140"),
                    }
    except ISolarCloudError as exc:
        raise _api_error(exc)
    return cache_put(f"batteries:{ps_id}", result)


@app.get("/api/plants/{ps_id}/flow")
async def plant_flow(ps_id: str):
    """Live energy flow: PV, house load, grid import/export, battery."""
    cached = cache_get(f"flow:{ps_id}", 60)
    if cached is not None:
        return cached
    try:
        devices = await cached_devices(ps_id)
        ess = next((d for d in devices if d.get("device_type") == 14), None)
        if ess is None:
            raise HTTPException(status_code=404, detail="No hybrid/ESS inverter in plant")
        flow = await fetch_flow(ess["ps_key"])
    except ISolarCloudError as exc:
        raise _api_error(exc)
    if flow is None:
        raise HTTPException(status_code=502, detail="No flow data from inverter")
    return cache_put(f"flow:{ps_id}", flow)


@app.get("/api/plants/{ps_id}/flow/history")
async def flow_history(ps_id: str, date: str | None = None):
    """Day series of flow samples from the local DB (collector, 5-min)."""
    date = date or datetime.now().strftime("%Y%m%d")
    return {"date": date, "samples": storage.load_flow_day(ps_id, date)}


# ---- EV (Tesla Wall Connector) --------------------------------------------

_ev_last: dict = {}


@app.post("/api/ev/ingest")
async def ev_ingest(request: Request):
    """Push endpoint for the home twc-agent. Bearer-token authenticated."""
    if not settings.ev_ingest_token:
        raise HTTPException(status_code=404, detail="EV ingest not configured")
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    import hmac as _hmac
    if not _hmac.compare_digest(token, settings.ev_ingest_token):
        raise HTTPException(status_code=401, detail="Bad token")
    payload = await request.json()
    sample = {
        "vehicle_connected": bool(payload.get("vehicle_connected")),
        "charging": bool(payload.get("charging")),
        "power_w": payload.get("power_w"),
        "voltage": payload.get("voltage"),
        "current_a": payload.get("current_a"),
        "session_wh": payload.get("session_wh"),
        "lifetime_wh": payload.get("lifetime_wh"),
    }
    ts = time.strftime("%Y%m%d%H%M%S")
    storage.store_ev(ts, sample)
    _ev_last.clear()
    _ev_last.update(sample, received=time.time())
    return {"ok": True}


@app.get("/api/ev")
async def ev_status():
    """Latest Wall Connector state: pushed by the agent, or polled locally."""
    if _ev_last and time.time() - _ev_last.get("received", 0) < 600:
        return {**{k: v for k, v in _ev_last.items() if k != "received"},
                "age_s": int(time.time() - _ev_last["received"])}
    if wallconnector.enabled():
        try:
            return {**(await wallconnector.fetch_status()), "age_s": 0}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Wall Connector: {exc}")
    raise HTTPException(status_code=404, detail="No EV data")


@app.get("/api/ev/history")
async def ev_history(date: str | None = None):
    date = date or datetime.now().strftime("%Y%m%d")
    return {"date": date, "samples": storage.load_ev_day(date)}


@app.get("/api/plants/{ps_id}/weather")
async def plant_weather(ps_id: str, date: str | None = None):
    """Hourly weather codes + current conditions for the plant's location,
    from Open-Meteo (no API key; only plant coordinates are sent)."""
    date = date or datetime.now().strftime("%Y%m%d")
    cached = cache_get(f"weather:{ps_id}:{date}", 900)
    if cached is not None:
        return cached
    try:
        plants = (await client.get_power_station_list()).get("pageList", [])
    except ISolarCloudError as exc:
        raise _api_error(exc)
    plant = next((p for p in plants if str(p.get("ps_id")) == ps_id), None)
    if plant is None or plant.get("latitude") is None:
        raise HTTPException(status_code=404, detail="Plant coordinates not available")

    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    async with httpx.AsyncClient(timeout=15, transport=make_transport()) as http:
        r = await http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": round(float(plant["latitude"]), 3),
                "longitude": round(float(plant["longitude"]), 3),
                "hourly": "weather_code",
                "current": "weather_code,temperature_2m",
                "timezone": "auto",
                "start_date": iso,
                "end_date": iso,
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    w = r.json()
    hourly = [
        {"h": int(t[11:13]), "code": c}
        for t, c in zip(w["hourly"]["time"], w["hourly"]["weather_code"])
    ]
    return cache_put(f"weather:{ps_id}:{date}", {
        "date": date,
        "current": {
            "code": w.get("current", {}).get("weather_code"),
            "temp_c": w.get("current", {}).get("temperature_2m"),
        },
        "hourly": hourly,
    })


@app.get("/api/plants/{ps_id}/batteries/history")
async def battery_history(ps_id: str, sn: str, date: str | None = None):
    """Battery SOC/SOH/temperature day series from the local DB."""
    date = date or datetime.now().strftime("%Y%m%d")
    return {"sn": sn, "date": date, "samples": storage.load_battery_day(sn, date)}


@app.get("/api/plants/{ps_id}/panels/history")
async def panel_history(ps_id: str, ps_key: str, date: str | None = None):
    """Per-panel power day series from the local DB."""
    date = date or datetime.now().strftime("%Y%m%d")
    return {"ps_key": ps_key, "date": date, "samples": storage.load_panel_day(ps_key, date)}
