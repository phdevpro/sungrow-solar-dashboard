# Sungrow Solar Dashboard

Self-hosted dashboard for Sungrow solar plants, built on the official
[iSolarCloud OpenAPI](https://developer-api.isolarcloud.eu). Not affiliated
with or endorsed by Sungrow.

Features:

- Plant KPIs: current power, daily/total energy, income, CO₂ savings
- 5-minute production curve with hover tooltip
- Per-panel output via SP optimizers (MLPE), with a "not communicating"
  flag for optimizers that never reported
- Battery storage: system state of charge plus per-battery SOC, SOH,
  temperature, voltage and current
- Device list with online status
- **Local history**: a background collector polls the API every 5 minutes
  and stores samples in SQLite, so historical queries are served from your
  own database instead of the rate-limited cloud API
- Hourly weather strip under the production chart plus current conditions
  in the header (Open-Meteo, keyless; only plant coordinates are sent)
- Optional login (set `DASH_USER` / `DASH_PASSWORD`) with signed session
  cookies — recommended when the dashboard is reachable from the internet
- Installable PWA (manifest + service worker): add it to your phone's home
  screen
- Optional Tesla Wall Connector Gen 3 integration: an EV node in the
  energy-flow diagram plus charge-session history, fed either by polling
  the unit on the same LAN (`TWC_HOST`) or by the bundled
  [`twc-agent`](twc-agent/) container running on your home server pushing
  to `/api/ev/ingest` (`EV_INGEST_TOKEN` shared secret) when the dashboard
  is hosted elsewhere

## Prerequisites

1. An iSolarCloud account (the one you use in the app / website).
2. Developer credentials from the [Sungrow Developer Portal](https://developer-api.isolarcloud.eu):
   - Log in with your iSolarCloud account and create an application
     (choose **No** for OAuth 2.0 — this project uses user-level login).
   - After the review passes you get an **Appkey** and a **Secret Key**.
   - Note: keys may take a few minutes after approval before the gateway
     accepts them (`er_invalid_appkey` until then).

## Configuration

```bash
cp .env.example .env
# edit .env: gateway region, appkey, access key, account credentials
```

## Run with Docker (recommended)

```bash
docker compose up -d
```

Open http://localhost:8000. Collected history lands in `./data/solar.db`.

### Prebuilt image / Portainer

Every push to `main` publishes a multi-arch image (linux/amd64,
linux/arm64 — Raspberry Pi 3+/4/5 with a 64-bit OS, Apple Silicon,
most NAS) to GHCR:

```
ghcr.io/phdevpro/sungrow-solar-dashboard:latest
```

For Portainer, create a stack from [`portainer-stack.yml`](portainer-stack.yml)
and set `ISC_APPKEY`, `ISC_ACCESS_KEY`, `ISC_USERNAME`, `ISC_PASSWORD`
as stack environment variables (adjust `ISC_GATEWAY` if you are not on
the European site). History is stored in the `solar_data` named volume.

To expose it on the internet without opening ports, put a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
in front and protect it with Cloudflare Access.

## Run locally (development)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API routes

| Route | Description |
|---|---|
| `GET /api/plants` | Plant list with KPIs |
| `GET /api/plants/{ps_id}/curve?date=YYYYMMDD` | 5-min production curve (DB-first, API backfill) |
| `GET /api/plants/{ps_id}/panels` | Per-panel optimizer realtime output |
| `GET /api/plants/{ps_id}/batteries` | Battery SOC/SOH snapshot |
| `GET /api/plants/{ps_id}/batteries/history?sn=...&date=...` | Battery day series from local DB |
| `GET /api/plants/{ps_id}/panels/history?ps_key=...&date=...` | Panel day series from local DB |
| `GET /api/plants/{ps_id}/devices` | Device list |

## Notes & limitations

- The gateway rate-limits aggressively on the free plan (HTTP 429). The
  client keeps concurrency at 2 and retries with backoff; responses are
  cached server-side (curve ~4.5 min, panels/batteries ~55 s).
- Minute-data queries are limited to ~2-hour windows; the day curve is
  chunked accordingly.
- Measuring points used: inverter/ESS production power `p13003` (type 14)
  or `p24` (type 1); optimizer `58107` output W / `58101` lifetime Wh;
  battery `58604` SOC / `58605` SOH / `58603` temperature / `58601` V /
  `58602` A; ESS system SOC `p13141` / SOH `p13142`.
- Tested with an SH-series hybrid inverter, SBS batteries and SP optimizers.
  Other setups may expose different measuring points — adjust as needed.

## License

MIT
