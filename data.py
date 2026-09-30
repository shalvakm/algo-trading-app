"""Instrument lookup and historical candle fetching."""
import difflib
import logging
import math
import time as _time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import NetworkException

from config import IST, MARKET_CLOSE, STATE_DIR, Settings

log = logging.getLogger(__name__)

# Kite's max date range per historical_data request, by interval
MAX_DAYS_PER_REQUEST = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}
HISTORICAL_RATE_DELAY = 0.35  # historical API allows ~3 requests/second

# Brief network glitches (Wi-Fi drop, "connection reset by peer", Kite 5xx) that are worth retrying
NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout, NetworkException)


def with_retry(fn, *args, attempts: int = 3, **kwargs):
    """Calls fn, retrying network errors with a short backoff (1s, 2s)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except NETWORK_ERRORS as e:
            if attempt == attempts:
                raise
            wait = 2 ** (attempt - 1)
            log.warning("Network error (%s), retry %d/%d in %ds",
                        type(e).__name__, attempt, attempts - 1, wait)
            _time.sleep(wait)


def load_instruments(kite: KiteConnect, exchange: str) -> pd.DataFrame:
    """Instrument dump for the exchange, cached once per day."""
    path = STATE_DIR / f"instruments_{exchange}_{date.today().isoformat()}.csv"
    if path.exists():
        return pd.read_csv(path)
    for old in STATE_DIR.glob(f"instruments_{exchange}_*.csv"):
        old.unlink()
    df = pd.DataFrame(kite.instruments(exchange))
    df.to_csv(path, index=False)
    return df


def resolve_instruments(kite: KiteConnect, settings: Settings) -> dict[str, dict]:
    """Maps each symbol -> {instrument_token, tick_size, lot_size}."""
    df = load_instruments(kite, settings.exchange)
    missing = [sym for sym in settings.symbols if not (df["tradingsymbol"] == sym).any()]
    if missing:
        lines = [f"  {sym}: not found on {settings.exchange}{_suggest(df, sym)}" for sym in missing]
        raise SystemExit("Fix SYMBOLS in .env - these symbols don't exist:\n" + "\n".join(lines))
    out = {}
    for sym in settings.symbols:
        r = df[df["tradingsymbol"] == sym].iloc[0]
        out[sym] = {
            "instrument_token": int(r["instrument_token"]),
            "tick_size": float(r["tick_size"]),
            "lot_size": int(r["lot_size"]),
        }
    return out


def resolve_index_token(kite: KiteConnect, exchange: str, index_name: str) -> int:
    """Instrument token of an index such as 'NIFTY 50' (used for the market filter)."""
    df = load_instruments(kite, exchange)
    row = df[(df["tradingsymbol"] == index_name) & (df["segment"] == "INDICES")]
    if row.empty:
        raise SystemExit(f"MARKET_INDEX '{index_name}' not found on {exchange}{_suggest(df, index_name)}")
    return int(row.iloc[0]["instrument_token"])


def _suggest(df: pd.DataFrame, sym: str) -> str:
    """Similar equity symbols, to help fix typos and renamed stocks."""
    eq = df[df["instrument_type"] == "EQ"] if "instrument_type" in df else df
    names = eq["tradingsymbol"].astype(str).tolist()
    close = difflib.get_close_matches(sym, names, n=3, cutoff=0.6)
    prefix = [n for n in names if n.startswith(sym[:4])][:3]
    hits = list(dict.fromkeys(close + prefix))[:4]
    return f" (did you mean: {', '.join(hits)}?)" if hits else " (check the name in Kite)"


def fetch_history(kite: KiteConnect, instrument_token: int, interval: str,
                  start: datetime, end: datetime) -> pd.DataFrame:
    """Fetches candles between start and end, splitting into allowed chunks."""
    chunk = timedelta(days=MAX_DAYS_PER_REQUEST[interval])
    frames = []
    cur = start
    while cur < end:
        stop = min(cur + chunk, end)
        rows = with_retry(kite.historical_data, instrument_token, cur, stop, interval)
        if rows:
            frames.append(pd.DataFrame(rows))
        cur = stop + timedelta(seconds=1)
        _time.sleep(HISTORICAL_RATE_DELAY)

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    df = df.drop_duplicates("date").set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def drop_incomplete_candle(df: pd.DataFrame, interval: str, interval_minutes: int,
                           now: datetime) -> pd.DataFrame:
    """Kite returns the still-forming candle too; strategies must only use closed ones."""
    if df.empty:
        return df
    last = df.index[-1]
    if interval == "day":
        incomplete = last.date() == now.date() and now.time() < MARKET_CLOSE
    else:
        # the last candle of the day may be shorter (e.g. 60minute: 15:15-15:30)
        candle_end = min(last + timedelta(minutes=interval_minutes),
                         last.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute))
        incomplete = candle_end > now
    return df.iloc[:-1] if incomplete else df


def recent_closed_candles(kite: KiteConnect, instrument_token: int, settings: Settings,
                          min_candles: int) -> pd.DataFrame:
    """Enough recent closed candles to compute the moving averages reliably."""
    now = datetime.now(IST)
    candles_per_day = max(1, 375 // settings.interval_minutes)
    trading_days = math.ceil(min_candles / candles_per_day)
    # x2 + 5 to cover weekends and holidays
    start = now - timedelta(days=trading_days * 2 + 5)
    df = fetch_history(kite, instrument_token, settings.interval, start, now)
    return drop_incomplete_candle(df, settings.interval, settings.interval_minutes, now)
