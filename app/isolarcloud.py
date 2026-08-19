"""Async client for the Sungrow iSolarCloud OpenAPI.

API docs: https://developer-api.isolarcloud.eu
All endpoints are POST requests to <gateway>/openapi/<name> with a JSON body
that always carries `appkey` (+ `token` after login), and the secret key in
the `x-access-key` header.
"""

import asyncio
import time
from typing import Any

import httpx

from .config import settings

SYS_CODE = "901"


def make_transport() -> httpx.AsyncHTTPTransport | None:
    """Binding the local address to 0.0.0.0 restricts httpx to IPv4."""
    if settings.force_ipv4:
        return httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return None


class ISolarCloudError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"iSolarCloud error {code}: {message}")


class ISolarCloudClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.gateway, timeout=30, transport=make_transport()
        )
        self._token: str | None = None
        self._token_time: float = 0.0
        # Tokens are valid for a while server-side; re-login proactively after 23h.
        self._token_ttl = 23 * 3600
        # The gateway rate-limits aggressively (429 on bursts): keep concurrency
        # low and retry with backoff.
        self._sem = asyncio.Semaphore(2)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, endpoint: str, payload: dict[str, Any], *, with_token: bool = True) -> Any:
        body: dict[str, Any] = {
            "appkey": settings.appkey,
            "lang": "_en_US",
            **payload,
        }
        if with_token:
            body["token"] = await self._get_token()

        headers = {
            "x-access-key": settings.access_key,
            "sys_code": SYS_CODE,
            "Content-Type": "application/json",
        }
        async with self._sem:
            for attempt in range(4):
                resp = await self._http.post(
                    f"/openapi/{endpoint}", json=body, headers=headers
                )
                if resp.status_code != 429:
                    break
                await asyncio.sleep(1.5 * 2**attempt)
        resp.raise_for_status()
        data = resp.json()

        code = str(data.get("result_code"))
        if code != "1":
            # Token expired/invalid -> drop it so the next call re-logs in.
            if code in ("E00003", "010", "011"):
                self._token = None
            raise ISolarCloudError(code, data.get("result_msg", "unknown error"))
        return data.get("result_data")

    async def _get_token(self) -> str:
        if self._token and (time.time() - self._token_time) < self._token_ttl:
            return self._token

        result = await self._call(
            "login",
            {
                "user_account": settings.username,
                "user_password": settings.password,
            },
            with_token=False,
        )
        if not result or result.get("login_state") not in ("1", 1):
            raise ISolarCloudError(
                str(result.get("login_state", "?")) if result else "?",
                result.get("msg", "login failed") if result else "login failed",
            )
        self._token = result["token"]
        self._token_time = time.time()
        return self._token

    # ---- Plants -----------------------------------------------------------

    async def get_power_station_list(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        return await self._call("getPowerStationList", {"curPage": page, "size": size})

    async def get_station_real_kpi(self, ps_ids: list[int | str]) -> list[dict[str, Any]]:
        """Real-time plant KPIs: current power, daily/total energy, income."""
        return await self._call(
            "getStationRealKpi",
            {"ps_id_list": [str(i) for i in ps_ids]},
        )

    async def get_power_station_detail(self, ps_id: int | str) -> dict[str, Any]:
        return await self._call("getPowerStationDetail", {"ps_id": ps_id, "sn": ""})

    # ---- Devices ----------------------------------------------------------

    async def get_device_list(self, ps_id: int | str, page: int = 1, size: int = 50) -> dict[str, Any]:
        return await self._call(
            "getDeviceList",
            {"ps_id": ps_id, "curPage": page, "size": size},
        )

    async def get_device_realtime_data(
        self,
        ps_key_list: list[str],
        point_id_list: list[str],
        device_type: int = 1,
    ) -> dict[str, Any]:
        """Real-time measuring points of devices.

        device_type 1 = inverter. Common inverter points:
        p24 daily yield (Wh), p14 total DC power (W), p18 total active power (W),
        p8062 total yield (Wh).
        """
        return await self._call(
            "getDeviceRealTimeData",
            {
                "ps_key_list": ps_key_list,
                "point_id_list": point_id_list,
                "device_type": device_type,
            },
        )

    # ---- History ----------------------------------------------------------

    async def get_device_minute_data(
        self,
        ps_key_list: list[str],
        points: str,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        """5-minute historical point data. start/end format: yyyyMMddHHmmss.
        The API rejects windows longer than ~2 hours, so callers must chunk.
        """
        return await self._call(
            "getDevicePointMinuteDataList",
            {
                "ps_key_list": ps_key_list,
                "points": points,
                "start_time_stamp": start,
                "end_time_stamp": end,
            },
        )

    async def get_day_curve(self, ps_key: str, point: str, date: str) -> list[dict[str, str]]:
        """Full-day 5-minute curve for one device point, chunked in 2h windows.

        date format: YYYYMMDD. Returns the merged, time-ordered sample list.
        """
        async def chunk(h: int):
            start = f"{date}{h:02d}0000"
            end = f"{date}{h + 1:02d}5959"
            try:
                r = await self.get_device_minute_data([ps_key], point, start, end)
                return r.get(ps_key, [])
            except ISolarCloudError:
                return []

        # Don't query hours that haven't happened yet.
        last_hour = 23
        if date == time.strftime("%Y%m%d"):
            last_hour = time.localtime().tm_hour
        results = await asyncio.gather(*[chunk(h) for h in range(0, last_hour + 1, 2)])
        seen: set[str] = set()
        samples = []
        for part in results:
            for row in part:
                ts = row.get("time_stamp")
                if ts and ts not in seen:
                    seen.add(ts)
                    samples.append(row)
        samples.sort(key=lambda r: r["time_stamp"])
        return samples

    # ---- Optimizers (MLPE) -------------------------------------------------

    async def get_optimizer_list(self, ps_id: int | str) -> list[dict[str, Any]]:
        result = await self._call("getMlpeDeviceList", {"ps_id": str(ps_id)})
        return result.get("device_list", [])

    async def get_optimizer_realtime(
        self, ps_key_list: list[str], point_id_list: list[str]
    ) -> list[dict[str, Any]]:
        """Optimizer points: 58101 total yield Wh, 58103 Vin, 58104 Vout,
        58105 Iin, 58106 Iout, 58107 output power W. Max 100 keys per call."""
        result = await self._call(
            "getMlpeRealTimeData",
            {"ps_key_list": ps_key_list, "point_id_list": point_id_list},
        )
        return [x["device_point"] for x in result.get("device_point_list", [])]


client = ISolarCloudClient()
