"""Cookie-session auth for the dashboard.

Enabled when DASH_USER and DASH_PASSWORD are set. The session cookie is a
signed expiry timestamp (HMAC-SHA256); no server-side session store needed.
"""

import hashlib
import hmac
import time

from .config import settings

SESSION_COOKIE = "session"
SESSION_DAYS = 30


def enabled() -> bool:
    return bool(settings.dash_user and settings.dash_password)


def _key() -> bytes:
    base = settings.dash_secret or (settings.dash_password + settings.appkey)
    return hashlib.sha256(("sungrow-dash:" + base).encode()).digest()


def make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        expected = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and int(exp) > time.time()
    except (ValueError, TypeError):
        return False


def check_credentials(user: str, password: str) -> bool:
    ok_user = hmac.compare_digest(user.encode(), settings.dash_user.encode())
    ok_pass = hmac.compare_digest(password.encode(), settings.dash_password.encode())
    return ok_user and ok_pass
