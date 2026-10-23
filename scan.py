"""Most active stocks scanner (read-only - places no orders).

    python scan.py              # top 20 by turnover and by unusual activity
    python scan.py --top 30     # longer lists
    python scan.py --csv        # also save the full table to logs/scan_YYYY-MM-DD_HHMM.csv

Universe: NSE stocks in the F&O segment (~200 of the most actively traded), minus
PROTECTED_SYMBOLS. During market hours it uses live quotes (today so far); after hours,
at weekends or on holidays it shows the last trading session.

  turnover   = rupees traded (volume x average price)
  vs_normal  = turnover against the stock's own 20-day average. During the day this is
               scaled by how much of the session has passed, so 2.0x at noon means the
               stock is trading at twice its usual pace.
"""
import argparse
import json
import logging
import time as _time
from datetime import datetime, timedelta

import pandas as pd

from config import IST, LOG_DIR, MARKET_CLOSE, MARKET_OPEN, STATE_DIR, load_settings
from data import fetch_history, load_instruments, with_retry

log = logging.getLogger("scan")
QUOTE_BATCH = 100  # Kite allows 500, but long request URLs get blocked by its firewall
SESSION_MINUTES = 375


def fno_universe(kite, protected: frozenset[str]) -> dict[str, int]:
    """{symbol: instrument_token} for NSE equities that have F&O contracts (cached per day)."""
    path = STATE_DIR / f"scan_universe_{datetime.now(IST).date()}.json"
    if path.exists():
        return json.loads(path.read_text())
    for old in STATE_DIR.glob("scan_universe_*.json"):
        old.unlink()
    nfo = pd.DataFrame(with_retry(kite.instruments, "NFO"))
    names = set(nfo.loc[nfo["instrument_type"] == "FUT", "name"].astype(str))
    eq = load_instruments(kite, "NSE")
    eq = eq[(eq["segment"] == "NSE") & (eq["instrument_type"] == "EQ") & eq["tradingsymbol"].astype(str).isin(names)]
    uni = {str(r.tradingsymbol): int(r.instrument_token) for r in eq.itertuples() if r.tradingsymbol not in protected}
    path.write_text(json.dumps(uni))
    return uni


def daily_baseline(kite, universe: dict[str, int]) -> pd.DataFrame:
    """Last completed session and 20-day average turnover per stock (cached per day)."""
    today = datetime.now(IST).date()
    path = STATE_DIR / f"scan_baseline_{today}.csv"
    if path.exists():
        return pd.read_csv(path, index_col="stock")
    for old in STATE_DIR.glob("scan_baseline_*.csv"):
        old.unlink()
    print(f"Building today's baseline for {len(universe)} stocks (about a minute, once per day)...")
    end = datetime.now(IST)
    rows = []
    for i, (sym, tok) in enumerate(universe.items(), 1):
        try:
            d = fetch_history(kite, tok, "day", end - timedelta(days=45), end)
        except Exception as e:
            log.warning("%s: history failed (%s)", sym, type(e).__name__)
            continue
        d = d[d.index.date < today]  # completed sessions only
        if len(d) < 21:
            continue
        turnover = d["close"] * d["volume"]
        rows.append({"stock": sym, "session": d.index[-1].strftime("%d-%b"), "close": float(d["close"].iloc[-1]),
                     "prev_close": float(d["close"].iloc[-2]), "turnover": float(turnover.iloc[-1]),
                     "avg20": float(turnover.iloc[-21:-1].mean())})
        if i % 50 == 0:
            print(f"  {i}/{len(universe)}")
    base = pd.DataFrame(rows).set_index("stock")
    base.to_csv(path)
    return base


def live_snapshot(kite, symbols: list[str]) -> pd.DataFrame:
    """Today's volume, average price and change from live quotes."""
    rows = []
    for i in range(0, len(symbols), QUOTE_BATCH):
        q = with_retry(kite.quote, [f"NSE:{x}" for x in symbols[i:i + QUOTE_BATCH]])
        for key, v in q.items():
            prev = (v.get("ohlc") or {}).get("close") or 0
            last = v.get("last_price") or 0
            rows.append({"stock": key.split(":", 1)[1], "close": last, "prev_close": prev,
                         "turnover": (v.get("volume") or 0) * (v.get("average_price") or 0)})
        _time.sleep(1.1)
    return pd.DataFrame(rows).set_index("stock")


def session_fraction(now: datetime) -> float:
    start = datetime.combine(now.date(), MARKET_OPEN, IST)
    return min(max((now - start).total_seconds() / 60 / SESSION_MINUTES, 0.0), 1.0)


def select_watchlist(kite, s) -> tuple[list[str], pd.DataFrame]:
    """Today's symbols for SYMBOL_MODE=scan: the SCAN_TOP F&O stocks with the highest turnover in the
    last session relative to their own 20-day average (unusual activity), price >= SCAN_MIN_PRICE.

    Decided once per day from completed sessions only; saved to logs/watchlist_YYYY-MM-DD.csv and
    reused on a restart the same day so the bot keeps a consistent list.
    """
    today = datetime.now(IST).date()
    path = LOG_DIR / f"watchlist_{today}.csv"
    if path.exists():
        picks = pd.read_csv(path)
        return picks["stock"].astype(str).tolist(), picks
    base = daily_baseline(kite, fno_universe(kite, s.protected_symbols))
    base = base[(base["close"] >= s.scan_min_price) & (base["avg20"] > 0)].copy()
    base["vs_normal"] = base["turnover"] / base["avg20"]
    base["chg_pct"] = (base["close"] / base["prev_close"] - 1) * 100
    base["turnover_cr"] = base["turnover"] / 1e7
    picks = base.sort_values("vs_normal", ascending=False).head(s.scan_top).reset_index()
    picks.insert(0, "rank", range(1, len(picks) + 1))
    picks = picks[["rank", "stock", "session", "vs_normal", "turnover_cr", "close", "chg_pct"]].round(2)
    picks.to_csv(path, index=False)
    return picks["stock"].astype(str).tolist(), picks


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Most active NSE stocks (read-only)")
    p.add_argument("--top", type=int, default=20, help="rows per list (default 20)")
    p.add_argument("--min-price", type=float, default=50, help="skip stocks below this price (default 50)")
    p.add_argument("--csv", action="store_true", help="also save the full table to logs/")
    a = p.parse_args()

    from auth import get_kite
    s = load_settings()
    kite = get_kite(s)
    universe = fno_universe(kite, s.protected_symbols)
    base = daily_baseline(kite, universe)

    now = datetime.now(IST)
    in_session = now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE
    df = live_snapshot(kite, list(base.index)) if in_session else None
    if df is not None and (df["turnover"] > 0).sum() >= len(df) // 2:
        frac = max(session_fraction(now), 15 / SESSION_MINUTES)  # avoid silly ratios right after 9:15
        df = df.join(base[["avg20"]])
        df["vs_normal"] = df["turnover"] / (df["avg20"] * frac)
        title = f"LIVE - today so far ({now:%a %d-%b %H:%M}, {frac * 100:.0f}% of the session)"
    else:
        df = base.copy()
        df["vs_normal"] = df["turnover"] / df["avg20"]
        title = f"LAST SESSION - {df['session'].mode()[0]} (market closed now)"

    df = df[(df["close"] >= a.min_price) & (df["turnover"] > 0)].copy()
    df["chg_%"] = (df["close"] / df["prev_close"] - 1) * 100
    df["turnover_cr"] = df["turnover"] / 1e7
    df["in_your_list"] = [("YES" if x in s.symbols else "") for x in df.index]
    cols = ["close", "chg_%", "turnover_cr", "vs_normal", "in_your_list"]
    fmt = {"close": "{:.2f}".format, "chg_%": "{:+.2f}".format, "turnover_cr": "{:,.0f}".format, "vs_normal": "{:.2f}x".format}

    print(f"\n{title} | {len(df)} F&O stocks\n")
    for heading, col in (("TOP BY TURNOVER (most rupees traded)", "turnover_cr"),
                         ("TOP BY UNUSUAL ACTIVITY (vs each stock's normal pace)", "vs_normal")):
        t = df.sort_values(col, ascending=False).head(a.top)[cols].reset_index()
        t.index = range(1, len(t) + 1)
        print(heading)
        print(t.to_string(formatters=fmt))
        print()
    top = set(df.sort_values("turnover_cr", ascending=False).head(a.top).index)
    print(f"Your stocks in the top {a.top} by turnover: {', '.join(sorted(top & set(s.symbols))) or 'none'}")

    if a.csv:
        out = LOG_DIR / f"scan_{now:%Y-%m-%d_%H%M}.csv"
        df.sort_values("turnover_cr", ascending=False)[cols].to_csv(out)
        print(f"Saved {out}")


if __name__ == "__main__":
    main()
