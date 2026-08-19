"""Tesla Wall Connector → dashboard push agent.

Runs on the home network next to the Wall Connector Gen 3: polls its
local API and pushes each sample to the dashboard's /api/ev/ingest.

Environment:
  TWC_HOST      Wall Connector IP/host on the LAN (required)
  DASH_URL      dashboard base URL, e.g. https://solar.example.com (required)
  INGEST_TOKEN  shared secret, must match the dashboard's EV_INGEST_TOKEN (required)
  INTERVAL      poll interval in seconds (default 60)
"""

import logging
import os
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("twc-agent")

TWC_HOST = os.environ["TWC_HOST"]
DASH_URL = os.environ["DASH_URL"].rstrip("/")
INGEST_TOKEN = os.environ["INGEST_TOKEN"]
INTERVAL = int(os.getenv("INTERVAL", "60"))


def read_wall_connector(http: httpx.Client) -> dict:
    vitals = http.get(f"http://{TWC_HOST}/api/1/vitals").json()
    lifetime = http.get(f"http://{TWC_HOST}/api/1/lifetime").json()
    volts = vitals.get("grid_v") or 0
    amps = vitals.get("vehicle_current_a") or 0
    return {
        "vehicle_connected": bool(vitals.get("vehicle_connected")),
        "charging": bool(vitals.get("contactor_closed")),
        "power_w": round(volts * amps, 1),
        "voltage": volts,
        "current_a": amps,
        "session_wh": vitals.get("session_energy_wh"),
        "lifetime_wh": lifetime.get("energy_wh"),
    }


def main() -> None:
    headers = {"Authorization": f"Bearer {INGEST_TOKEN}"}
    with httpx.Client(timeout=10) as http:
        while True:
            try:
                sample = read_wall_connector(http)
                r = http.post(f"{DASH_URL}/api/ev/ingest", json=sample, headers=headers)
                r.raise_for_status()
                log.info(
                    "pushed: connected=%s charging=%s power=%.0fW session=%sWh",
                    sample["vehicle_connected"], sample["charging"],
                    sample["power_w"], sample["session_wh"],
                )
            except Exception as exc:
                log.warning("cycle failed: %s", exc)
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
