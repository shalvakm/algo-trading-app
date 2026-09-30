"""Kite Connect login.

Kite access tokens expire every day (~6 AM IST). This module caches the token
in state/access_token.json and only asks you to log in again when it's stale.

Run standalone to log in:  python auth.py
"""
import json
import logging
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from config import IST, STATE_DIR, Settings, load_settings

log = logging.getLogger(__name__)
TOKEN_FILE = STATE_DIR / "access_token.json"


def _last_expiry(now: datetime) -> datetime:
    """Most recent 6 AM IST - tokens created before this are expired."""
    six = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if now >= six else six - timedelta(days=1)


def _load_cached_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text())
    created = datetime.fromisoformat(data["created_at"])
    if created < _last_expiry(datetime.now(IST)):
        return None
    return data["access_token"]


def _extract_request_token(user_input: str) -> str:
    """Accepts either the raw request_token or the full redirect URL."""
    user_input = user_input.strip()
    if user_input.startswith("http"):
        params = parse_qs(urlparse(user_input).query)
        if "request_token" not in params:
            raise ValueError("No request_token found in that URL")
        return params["request_token"][0]
    return user_input


def interactive_login(kite: KiteConnect, settings: Settings) -> str:
    url = kite.login_url()
    print("\n1. Log in to Kite in your browser:\n   " + url)
    print("2. After login you'll be redirected to your app's redirect URL.")
    print("3. Paste that full redirect URL (or just the request_token) below.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    request_token = _extract_request_token(input("Redirect URL / request_token: "))
    session = kite.generate_session(request_token, api_secret=settings.api_secret)
    token = session["access_token"]
    TOKEN_FILE.write_text(json.dumps({
        "access_token": token,
        "created_at": datetime.now(IST).isoformat(),
    }))
    TOKEN_FILE.chmod(0o600)
    log.info("Logged in as %s", session.get("user_name", session.get("user_id")))
    return token


def get_kite(settings: Settings | None = None) -> KiteConnect:
    """Returns an authenticated KiteConnect client, logging in if needed."""
    settings = settings or load_settings()
    if not settings.api_key or settings.api_key == "your_api_key":
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in .env first.")

    kite = KiteConnect(api_key=settings.api_key)
    token = _load_cached_token()
    if token:
        kite.set_access_token(token)
        try:
            kite.profile()
            return kite
        except TokenException:
            log.warning("Cached access token rejected, logging in again")

    kite.set_access_token(interactive_login(kite, settings))
    return kite


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    k = get_kite()
    print("Authenticated:", k.profile()["user_name"])
