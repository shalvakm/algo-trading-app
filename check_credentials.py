"""Checks that your Kite credentials work. Read-only: places NO orders.

    python check_credentials.py

Logs in (reusing today's cached token if there is one), then makes a few
read-only API calls: profile, funds, holdings, a live quote and a small
historical data request.
"""
import logging
from datetime import datetime, timedelta

from auth import get_kite
from config import IST, load_settings


def check(name, fn):
    try:
        result = fn()
        print(f"  [OK]   {name}: {result}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        return False


def main():
    logging.basicConfig(level=logging.WARNING)
    s = load_settings()
    print(f"API key loaded: {s.api_key[:4]}...{s.api_key[-2:]} (secret hidden)\n")

    kite = get_kite(s)
    print("\nRunning read-only checks:")

    def profile():
        p = kite.profile()
        return f"{p['user_name']} ({p['user_id']}), exchanges={p['exchanges']}"

    def funds():
        eq = kite.margins("equity")
        return f"available cash = ₹{eq['available']['live_balance']:,.2f}"

    def holdings():
        h = kite.holdings()
        names = [x["tradingsymbol"] for x in h]
        missing = sorted(s.protected_symbols - set(names))
        note = f" | protected not found in holdings: {missing}" if missing else " | all protected symbols found"
        return f"{len(h)} holdings {names}{note}"

    def quote():
        keys = [f"{s.exchange}:{sym}" for sym in s.symbols]
        data = kite.ltp(keys)
        return ", ".join(f"{k.split(':')[1]}=₹{v['last_price']}" for k, v in data.items())

    def historical():
        from data import resolve_instruments
        from dataclasses import replace
        sym = s.symbols[0]
        token = resolve_instruments(kite, replace(s, symbols=[sym]))[sym]["instrument_token"]
        end = datetime.now(IST)
        rows = kite.historical_data(token, end - timedelta(days=7), end, "day")
        return f"{len(rows)} daily candles for {sym}"

    results = [
        check("Profile", profile),
        check("Funds", funds),
        check("Holdings", holdings),
        check("Live prices (LTP)", quote),
        check("Historical data", historical),
    ]

    print()
    if all(results):
        print("All checks passed - credentials are working.")
    else:
        print("Some checks failed. A failed 'Historical data' check usually means your "
              "Kite Connect plan doesn't include historical data, which the bot needs.")


if __name__ == "__main__":
    main()
