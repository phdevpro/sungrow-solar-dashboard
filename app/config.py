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
