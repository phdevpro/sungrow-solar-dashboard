"""Renault (MyRenault / Kamereon) client for the EV — tested with Zoe ZE40.

Enabled when RENAULT_EMAIL and RENAULT_PASSWORD are set. The ZE40 has no
direct "stop charge" command; pausing works by switching the charge mode
to schedule (with no active window), and resuming by switching back to
"always" plus an explicit charge-start.
"""

import logging
import time
from typing import Any

import aiohttp
from renault_api.renault_client import RenaultClient

from .config import settings

log = logging.getLogger("renault")


def enabled() -> bool:
    return bool(settings.renault_email and settings.renault_password)


class RenaultCar:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._vehicle = None
        self._login_time = 0.0

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        self._vehicle = None

    async def _get_vehicle(self):
        # Re-login every 6h; gigya sessions expire silently.
        if self._vehicle is not None and time.time() - self._login_time < 6 * 3600:
            return self._vehicle
        if self._session is None:
            self._session = aiohttp.ClientSession()
        client = RenaultClient(websession=self._session, locale=settings.renault_locale)
        await client.session.login(settings.renault_email, settings.renault_password)
        person = await client.get_person()
        account_id = next(
            a.accountId for a in person.accounts if a.accountType == "MYRENAULT"
        )
        account = await client.get_api_account(account_id)
        vehicles = await account.get_vehicles()
        vin = settings.renault_vin or vehicles.vehicleLinks[0].vin
        self._vehicle = await account.get_api_vehicle(vin)
        self._login_time = time.time()
        log.info("renault: logged in, vehicle %s", vin[-6:])
        return self._vehicle

    async def _retrying(self, fn):
        try:
            return await fn(await self._get_vehicle())
        except Exception:
            # One retry with a fresh login (expired session, transient error).
            self._vehicle = None
            return await fn(await self._get_vehicle())

    async def status(self) -> dict[str, Any]:
        async def call(vehicle):
            b = await vehicle.get_battery_status()
            return {
                "soc": b.batteryLevel,                     # percent
                "autonomy_km": b.batteryAutonomy,
                "plugged": b.plugStatus == 1,
                "charging": b.chargingStatus == 1.0,
                "charging_power_kw": b.chargingInstantaneousPower,
                "battery_temp_c": b.batteryTemperature,
                "updated": b.timestamp,
            }
        return await self._retrying(call)

    async def charge_mode(self) -> str | None:
        async def call(vehicle):
            m = await vehicle.get_charge_mode()
            return m.chargeMode
        return await self._retrying(call)

    async def resume_charge(self) -> None:
        """Mode 'always' + explicit start."""
        async def call(vehicle):
            await vehicle.set_charge_mode("always")
            try:
                await vehicle.set_charge_start()
            except Exception as exc:
                # Some firmwares reject start when already charging — harmless.
                log.info("charge_start after mode switch: %s", exc)
        await self._retrying(call)

    async def pause_charge(self) -> None:
        """ZE40 has no stop command: schedule mode with no window pauses it."""
        async def call(vehicle):
            await vehicle.set_charge_mode("schedule_mode")
        await self._retrying(call)


car = RenaultCar()
