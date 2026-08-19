# iSolarCloud Dashboard

Small FastAPI web app that reads data from your Sungrow solar plant via the
[iSolarCloud OpenAPI](https://developer-api.isolarcloud.eu) and shows it on a
local dashboard: current power, daily / monthly / total energy, and the device
list per plant. Auto-refreshes every 60 s.

## Prerequisites

1. An iSolarCloud account (the one you use in the app / website).
2. Developer credentials from https://developer-api.isolarcloud.eu:
   - Register / log in on the developer portal.
   - Create an application → you get an **Appkey** and a **Secret Key**
     (used as the `x-access-key` header).
   - Wait for the application to be approved if required.

## Setup

```bash
cd ~/repos/isolarcloud
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: appkey, access key, username, password, gateway
```

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## API routes

| Route | Description |
|---|---|
| `GET /api/plants` | Plant list (`getPowerStationList`) |
| `GET /api/plants/{ps_id}/kpi` | Real-time KPIs (`getStationRealKpi`) |
| `GET /api/plants/{ps_id}/detail` | Plant detail (`getPowerStationDetail`) |
| `GET /api/plants/{ps_id}/devices` | Devices (`getDeviceList`) |

## Notes

- The client logs in lazily and caches the token for ~23 h; on token-expiry
  error codes it re-logs in on the next request.
- Field names in responses occasionally differ between accounts/API versions;
  the dashboard tries the common variants (`curr_power`/`power`,
  `today_energy`/`actual_energy`). If a KPI shows `—`, check the raw JSON at
  `/api/plants/{ps_id}/kpi` and adjust `kpiHTML()` in
  `app/static/index.html`.
- If your developer application was created with **encryption enabled**
  (RSA/AES), the plain JSON calls in `app/isolarcloud.py` will fail with an
  auth error — either create the app without encryption or add the
  encryption layer per the portal docs.
- Never commit `.env` (see `.gitignore`).
