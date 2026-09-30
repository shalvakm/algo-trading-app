"""Live price feed over Kite's WebSocket (KiteTicker).

Keeps the latest traded price per symbol and calls `on_price(symbol, price)` for
every tick. The callback runs on the WebSocket thread, so it must be quick and
must not place orders itself (main.py hands exits to a worker thread).
"""
import logging
import threading
import time as _time
from typing import Callable

from kiteconnect import KiteTicker
from twisted.internet import reactor

log = logging.getLogger(__name__)


class TickerFeed:
    def __init__(self, api_key: str, access_token: str, token_to_symbol: dict[int, str],
                 on_price: Callable[[str, float], None]):
        self.api_key = api_key
        self.token_to_symbol = token_to_symbol
        self.on_price = on_price
        self.prices: dict[str, float] = {}
        self.last_tick_at: float | None = None
        self._lock = threading.Lock()
        self._kws = self._build(access_token)

    def _build(self, access_token: str) -> KiteTicker:
        kws = KiteTicker(self.api_key, access_token)
        kws.on_connect = self._on_connect
        kws.on_ticks = self._on_ticks
        kws.on_close = lambda ws, code, reason: log.warning("Ticker closed: %s %s", code, reason)
        kws.on_error = lambda ws, code, reason: log.error("Ticker error: %s %s", code, reason)
        kws.on_reconnect = lambda ws, n: log.warning("Ticker reconnecting (attempt %d)", n)
        kws.on_noreconnect = lambda ws: log.error("Ticker gave up reconnecting - "
                                                  "exits fall back to candle-close checks")
        return kws

    # ---------- callbacks (WebSocket thread) ----------
    def _on_connect(self, ws, response):
        tokens = list(self.token_to_symbol)
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_LTP, tokens)
        log.info("Ticker connected, streaming %s", sorted(self.token_to_symbol.values()))

    def _on_ticks(self, ws, ticks):
        now = _time.time()
        for t in ticks:
            sym = self.token_to_symbol.get(t["instrument_token"])
            if sym is None:
                continue
            price = float(t["last_price"])
            with self._lock:
                self.prices[sym] = price
                self.last_tick_at = now
            try:
                self.on_price(sym, price)
            except Exception:
                log.exception("on_price callback failed for %s", sym)

    # ---------- control (main thread) ----------
    def start(self) -> None:
        self._kws.connect(threaded=True)

    def restart(self, access_token: str) -> None:
        """Reconnect with a new access token (after the daily re-login).

        Twisted's reactor can't be restarted, so the old connection is closed and a new
        one opened on the already-running reactor thread.
        """
        old, self._kws = self._kws, self._build(access_token)
        reactor.callFromThread(old.close)
        reactor.callFromThread(self._kws.connect, threaded=True)
        log.info("Ticker restarting with new access token")

    def stop(self) -> None:
        if reactor.running:
            reactor.callFromThread(self._kws.close)

    def price(self, sym: str) -> float | None:
        with self._lock:
            return self.prices.get(sym)

    def seconds_since_last_tick(self) -> float | None:
        with self._lock:
            return None if self.last_tick_at is None else _time.time() - self.last_tick_at
