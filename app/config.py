import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    gateway: str = os.getenv("ISC_GATEWAY", "https://gateway.isolarcloud.eu").rstrip("/")
    appkey: str = os.getenv("ISC_APPKEY", "")
    access_key: str = os.getenv("ISC_ACCESS_KEY", "")
    username: str = os.getenv("ISC_USERNAME", "")
    password: str = os.getenv("ISC_PASSWORD", "")

    # Force outbound IPv4 (default on): the iSolarCloud gateway publishes
    # AAAA records, and containers without working IPv6 hang in
    # ConnectTimeout when the resolver hands out the v6 address first.
    force_ipv4: bool = os.getenv("ISC_FORCE_IPV4", "1").lower() not in ("0", "false", "no")

    # Tesla Wall Connector Gen 3 (optional): host/IP of its local API,
    # for when this app runs on the same LAN as the unit.
    twc_host: str = os.getenv("TWC_HOST", "")

    # Shared secret for the remote EV agent (twc-agent) pushing Wall
    # Connector data to /api/ev/ingest. Ingest is disabled when unset.
    ev_ingest_token: str = os.getenv("EV_INGEST_TOKEN", "")

    # Miele 3rd Party API (developer.miele.com): appliance status, remote
    # start, and solar-surplus auto-start. Register an app there and set
    # the redirect URI to https://<your-dashboard>/api/miele/callback.
    miele_client_id: str = os.getenv("MIELE_CLIENT_ID", "")
    miele_client_secret: str = os.getenv("MIELE_CLIENT_SECRET", "")
    # Auto-start appliances waiting for remote start when grid export
    # exceeds this threshold (default off; UI can toggle).
    miele_auto: bool = os.getenv("MIELE_AUTO", "0").lower() in ("1", "true", "yes")
    miele_auto_surplus_w: float = float(os.getenv("MIELE_AUTO_SURPLUS_W", "1000"))
    # Don't auto-start unless the house battery is at least this charged
    # (fraction, 0.2 = 20%).
    miele_auto_min_soc: float = float(os.getenv("MIELE_AUTO_MIN_SOC", "0.2"))

    # eWeLink / Sonoff (dev.ewelink.cc OAuth app). Redirect URI to register:
    # https://<your-dashboard>/api/ewelink/callback
    ewelink_client_id: str = os.getenv("EWELINK_CLIENT_ID", "")
    ewelink_client_secret: str = os.getenv("EWELINK_CLIENT_SECRET", "")
    ewelink_region: str = os.getenv("EWELINK_REGION", "eu")  # eu | us | as | cn

    # Dashboard login (optional): auth is enabled when both are set.
    dash_user: str = os.getenv("DASH_USER", "")
    dash_password: str = os.getenv("DASH_PASSWORD", "")
    # Session-signing secret; falls back to a hash of credentials so
    # sessions survive restarts without extra configuration.
    dash_secret: str = os.getenv("DASH_SECRET", "")

    def validate(self) -> list[str]:
        missing = []
        for field in ("appkey", "access_key", "username", "password"):
            if not getattr(self, field):
                missing.append(f"ISC_{field.upper()}")
        return missing


settings = Settings()
