import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    gateway: str = os.getenv("ISC_GATEWAY", "https://gateway.isolarcloud.eu").rstrip("/")
    appkey: str = os.getenv("ISC_APPKEY", "")
    access_key: str = os.getenv("ISC_ACCESS_KEY", "")
    username: str = os.getenv("ISC_USERNAME", "")
    password: str = os.getenv("ISC_PASSWORD", "")

    def validate(self) -> list[str]:
        missing = []
        for field in ("appkey", "access_key", "username", "password"):
            if not getattr(self, field):
                missing.append(f"ISC_{field.upper()}")
        return missing


settings = Settings()
