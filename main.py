"""Live / paper trading loop for the configured strategy (STRATEGY in .env).

    python main.py            # uses PAPER_TRADING from .env (default: paper)

Every time a candle closes, it fetches the latest closed candles for each
symbol, checks for a fresh strategy signal and moves the position accordingly.
MIS positions are squared off at SQUARE_OFF_TIME.

If STOP_LOSS_PCT / TARGET_PCT are set, a WebSocket feed watches live prices and
exits a position the moment either level is hit, without waiting for the candle.
"""
import logging
import queue
import sys
import threading
import time as _time
from datetime import datetime, timedelta

from kiteconnect.exceptions import NetworkException, TokenException

from auth import get_kite, interactive_login
from broker import Broker
from config import IST, LOG_DIR, MARKET_CLOSE, MARKET_OPEN, Settings, load_settings
from data import NETWORK_ERRORS, recent_closed_candles, resolve_index_token, resolve_instruments
from strategy import (add_signals, exit_levels, exit_reason, market_allows, market_trend,
                      min_candles_needed, target_position)
from shadow import ShadowBook
from sizing import position_size
from ticker import TickerFeed

CANDLE_SETTLE_SECONDS = 5  # wait after candle close so Kite has the final candle
log = logging.getLogger("bot")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.FileHandler(LOG_DIR / f"bot_{datetime.now(IST).date()}.log")
    file.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def at(day: datetime, t) -> datetime:
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def next_weekday(d: datetime) -> datetime:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_run_time(now: datetime, s: Settings) -> datetime:
    """When the next candle closes (plus a settle delay)."""
    settle = timedelta(seconds=CANDLE_SETTLE_SECONDS)
    is_trading_day = now.weekday() < 5

    if s.interval == "day":
        # Daily candle closes at 15:30; act on it at the next session's open,
        # matching the backtest (signal on close, fill at next open).
        run = at(now, MARKET_OPEN) + timedelta(seconds=20)
        if not is_trading_day or now >= run:
            run = at(next_weekday(now), MARKET_OPEN) + timedelta(seconds=20)
        return run

    step = timedelta(minutes=s.interval_minutes)
    open_ = at(now, MARKET_OPEN)
    close = at(now, MARKET_CLOSE)
    if is_trading_day and now < open_ + step + settle:
        return open_ + step + settle
    if is_trading_day and now < close + settle:
        n = int((now - open_) / step) + 1
        return min(open_ + n * step, close) + settle
    return at(next_weekday(now), MARKET_OPEN) + step + settle


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(IST)).total_seconds()
        if remaining <= 0:
            return
        _time.sleep(min(remaining, 30))


class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.kite = get_kite(settings)
        self.instruments = resolve_instruments(self.kite, settings)
        self.broker = Broker(self.kite, settings, self.instruments)
        self.min_candles = settings.strategy.min_candles()
        self.last_processed: dict[str, datetime] = {}
        self.squared_off_on = None
        self.intraday = settings.product == "MIS" and settings.interval != "day"

        # Market filter: overall index trend, refreshed at every candle check
        self.market_dir: int | None = None
        self.market_token = (resolve_index_token(self.kite, settings.exchange, settings.market_index)
                             if settings.market_filter else None)

        # News veto (NEWS_MODE=veto): per-stock entry blocks for the day, set once the briefing is ready
        self.news_vetoes: dict[str, dict[str, str]] | None = None
        self.news_vetoes_date = None
        self.shadow = ShadowBook(settings) if settings.news_mode == "veto" else None

        # Serialises every position change: candle loop, tick exits and square-off
        self.lock = threading.RLock()
        self.exit_queue: queue.Queue = queue.Queue()
        self.pending_exits: set[str] = set()
        self.feed: TickerFeed | None = None
        if settings.tick_exits_enabled:
            threading.Thread(target=self._exit_worker, daemon=True, name="exit-worker").start()
            token_to_symbol = {v["instrument_token"]: k for k, v in self.instruments.items()}
            self.feed = TickerFeed(settings.api_key, self.kite.access_token, token_to_symbol, self.on_price)
            self.feed.start()

    # ---------- live-tick exits ----------
    def _exit_hit(self, sym: str, price: float) -> str | None:
        pos = self.broker.positions.get(sym)
        if not pos or not pos["qty"]:
            return None
        direction = 1 if pos["qty"] > 0 else -1
        stop, target = exit_levels(pos["avg_price"], direction, self.s.stop_loss_pct, self.s.target_pct)
        return exit_reason(price, direction, stop, target)

    def on_price(self, sym: str, price: float) -> None:
        """Runs on the WebSocket thread for every tick: only detects, never trades."""
        if sym in self.pending_exits:
            return
        reason = self._exit_hit(sym, price)
        if reason:
            self.pending_exits.add(sym)
            self.exit_queue.put((sym, reason, price))

    def _exit_worker(self) -> None:
        while True:
            sym, reason, price = self.exit_queue.get()
            try:
                with self.lock:
                    # the position may have changed since the tick was queued
                    if self._exit_hit(sym, price) == reason:
                        log.info("%s: %s hit at %.2f", sym, reason, price)
                        self.broker.set_target(sym, 0, reason)
            except Exception:
                log.exception("%s: exit order failed", sym)
            finally:
                self.pending_exits.discard(sym)

    def check_exits_fallback(self) -> None:
        """Backup for the WebSocket: check open positions against a REST price every candle."""
        if not self.s.tick_exits_enabled:
            return
        open_syms = [sym for sym, pos in list(self.broker.positions.items()) if pos["qty"]]
        if not open_syms:
            return
        age = self.feed.seconds_since_last_tick() if self.feed else None
        if age is None or age > 120:
            log.warning("No live ticks for %s - relying on candle-close exit checks",
                        "a while" if age is None else f"{age:.0f}s")
        for sym in open_syms:
            try:
                self.on_price(sym, self.broker.ltp(sym))
            except Exception:
                log.exception("%s: fallback exit check failed", sym)

    def process_symbol(self, sym: str, now: datetime) -> None:
        token = self.instruments[sym]["instrument_token"]
        df = recent_closed_candles(self.kite, token, self.s, self.min_candles)
        if len(df) < self.min_candles:
            log.warning("%s: only %d candles, need %d", sym, len(df), self.min_candles)
            return

        last_ts = df.index[-1]
        if self.last_processed.get(sym) == last_ts:
            return  # no new candle (holiday, or already handled)
        first_look = sym not in self.last_processed
        self.last_processed[sym] = last_ts

        row = add_signals(df, self.s.strategy).iloc[-1]
        if self.shadow:
            self.shadow.on_candle(sym, row, int(row["signal"]), now)  # before any new veto this candle
        with self.lock:
            self._act_on_candle(sym, row, last_ts, first_look, now)

    def _act_on_candle(self, sym, row, last_ts, first_look, now) -> None:
        current = self.broker.position_dir(sym)
        log.info("%s candle %s close=%.2f %s signal=%+d pos=%+d",
                 sym, last_ts.strftime("%d-%b %H:%M"), row["close"], self.s.strategy.describe(row),
                 row["signal"], current)

        if row["signal"] == 0:
            return
        # On startup, ignore a signal that happened long ago - we'd be entering late
        if first_look and self.intraday:
            age = now - (last_ts + timedelta(minutes=self.s.interval_minutes))
            if age > timedelta(minutes=self.s.interval_minutes):
                log.info("%s: signal at %s is stale, waiting for the next one", sym, last_ts)
                return

        target = target_position(int(row["signal"]), current, self.s.allow_short)
        if self.s.market_filter and not market_allows(target, current, self.market_dir):
            mood = {1: "UP", -1: "DOWN"}.get(self.market_dir, "UNKNOWN")
            log.info("%s: %s but market (%s) is %s - %s", sym,
                     self.s.strategy.reason(int(row["signal"])),
                     self.s.market_index, mood, "exiting only" if current else "not entering")
            target = 0
        cutoff = self.s.no_new_entries_after
        if target not in (0, current) and cutoff and self.s.interval != "day" and now.time() >= cutoff:
            log.info("%s: %s after %s - no new entries, %s", sym,
                     self.s.strategy.reason(int(row["signal"])),
                     cutoff.strftime("%H:%M"), "exiting only" if current else "skipping")
            target = 0
        target = self._apply_news_veto(sym, target, current, row, now)
        if target != current:
            reason = self.s.strategy.reason(int(row["signal"]))
            self.broker.set_target(sym, target, reason)

    # ---------- news veto ----------
    def set_news_vetoes(self, analysis: dict | None) -> None:
        """Called by the briefing thread when today's briefing is ready."""
        from news import build_vetoes
        if not analysis:
            log.warning("News veto: no briefing today - trading without news vetoes")
            return
        self.news_vetoes, self.news_vetoes_date = build_vetoes(analysis), datetime.now(IST).date()
        if not self.news_vetoes:
            log.info("News veto: active - no stocks blocked today")
        for sym, block in sorted(self.news_vetoes.items()):
            what = "no new trades" if len(block) == 2 else f"no {next(iter(block))}s"
            log.info("News veto: %s %s - %s", sym, what, next(iter(block.values())))

    def _apply_news_veto(self, sym: str, target: int, current: int, row, now: datetime) -> int:
        """Blocks a NEW position the briefing vetoed (exits are never blocked). Shadow-tracks it."""
        if self.s.news_mode != "veto" or target in (0, current):
            return target
        if not self.news_vetoes or self.news_vetoes_date != now.date():
            return target  # briefing not ready / failed / from another day: trade normally
        side = "long" if target > 0 else "short"
        reason = self.news_vetoes.get(sym, {}).get(side)
        if not reason:
            return target
        log.info("%s: %s skipped - news veto (%s)", sym, self.s.strategy.reason(target), reason)
        if self.shadow and self.broker.trades_today < self.s.max_trades_per_day:
            try:
                price = self.broker.ltp(sym)
            except Exception:
                price = float(row["close"])
            self.shadow.start(sym, target, price, position_size(price, self.s), now, reason)
        return 0

    def update_market(self) -> None:
        """Refreshes the overall market direction from the index's latest closed candle."""
        if not self.s.market_filter:
            return
        period = self.s.market_ma_period
        try:
            df = recent_closed_candles(self.kite, self.market_token, self.s, min_candles_needed(period, "EMA"))
            trend = market_trend(df, period)
            last = trend.iloc[-1] if len(trend) else float("nan")
            self.market_dir = None if last != last else int(last)  # NaN -> None
            ema = df["close"].ewm(span=period, adjust=False).mean().iloc[-1] if len(df) else float("nan")
            log.info("Market %s candle %s close=%.2f EMA%d=%.2f -> %s", self.s.market_index,
                     df.index[-1].strftime("%d-%b %H:%M") if len(df) else "-",
                     df["close"].iloc[-1] if len(df) else float("nan"), period, ema,
                     {1: "UP (longs allowed)", -1: "DOWN (no new longs)"}.get(self.market_dir,
                                                                             "UNKNOWN (no new entries)"))
        except (TokenException, KeyboardInterrupt):
            raise
        except Exception as e:
            self.market_dir = None
            log.warning("Market filter: couldn't fetch %s (%s) - no new entries this candle",
                        self.s.market_index, e)

    def tick(self) -> None:
        now = datetime.now(IST)
        if self.intraday and now.time() >= self.s.square_off_time:
            return  # no new signals after square-off
        self.update_market()
        for sym in self.s.symbols:
            try:
                self.process_symbol(sym, now)
            except (TokenException, KeyboardInterrupt):
                raise
            except NETWORK_ERRORS as e:
                log.warning("%s: skipped this candle - network error after retries: %s", sym, e)
            except Exception:
                log.exception("%s: error while processing", sym)
        self.check_exits_fallback()

    def maybe_square_off(self) -> None:
        now = datetime.now(IST)
        if (self.intraday and now.weekday() < 5
                and self.s.square_off_time <= now.time() < MARKET_CLOSE
                and self.squared_off_on != now.date()):
            log.info("Square-off time reached")
            with self.lock:
                self.broker.square_off_all()
            if self.shadow and self.shadow.open:
                prices = {}
                for sym in list(self.shadow.open):
                    try:
                        prices[sym] = self.broker.ltp(sym)
                    except Exception as e:
                        log.warning("Shadow: no price for %s at square-off (%s)", sym, e)
                self.shadow.square_off(prices, now)
            self.squared_off_on = now.date()

    def run(self) -> None:
        while True:
            try:
                self.maybe_square_off()
                now = datetime.now(IST)
                nxt = next_run_time(now, self.s)
                if self.intraday and self.squared_off_on != now.date():
                    sq = at(now, self.s.square_off_time)
                    if now.weekday() < 5 and now < sq < nxt:
                        nxt = sq
                log.info("Next check at %s", nxt.strftime("%a %d-%b %H:%M:%S"))
                sleep_until(nxt)
                self.maybe_square_off()
                self.tick()
            except TokenException:
                log.error("Access token expired/invalid - please log in again")
                token = interactive_login(self.kite, self.s)
                self.kite.set_access_token(token)
                if self.feed:
                    self.feed.restart(token)
            except NetworkException as e:
                log.warning("Network error: %s - retrying in 30s", e)
                _time.sleep(30)


def main() -> None:
    setup_logging()
    s = load_settings()

    banner = "PAPER TRADING (no real orders)" if s.paper_trading else "LIVE TRADING - REAL MONEY"
    log.info("=" * 60)
    log.info("Trading bot | %s | %s", s.strategy.label, banner)
    log.info("Symbols=%s Exchange=%s Interval=%s Strategy=%s Product=%s Short=%s",
             s.symbols, s.exchange, s.interval, s.strategy.label, s.product, s.allow_short)
    log.info("Money limits: per trade %s, all open positions %s, daily loss %s",
             f"Rs {s.max_capital_per_trade:.0f}" if s.max_capital_per_trade else "no limit",
             f"Rs {s.max_total_capital:.0f}" if s.max_total_capital else "no limit",
             f"Rs {s.max_daily_loss:.0f}" if s.max_daily_loss else "no limit")
    log.info("Direction: %s", "LONG + SHORT (shorts open on SELL signals)" if s.allow_short
             else "LONG only (set ALLOW_SHORT=true to enable shorts)")
    log.info("Sizing: %s%s%s",
             f"risk Rs {s.risk_per_trade:.0f} per trade" if s.risk_per_trade else f"{s.quantity} shares per trade",
             f", max Rs {s.max_capital_per_trade:.0f} per position" if s.max_capital_per_trade else "",
             f", paper funds Rs {s.paper_funds:.0f}" if s.paper_trading else ", using real Kite funds")
    if s.no_new_entries_after:
        log.info("No new entries after %s", s.no_new_entries_after.strftime("%H:%M"))
    log.info("Market filter: %s", f"on - follow {s.market_index} vs its EMA{s.market_ma_period}"
             if s.market_filter else "off")
    log.info("News briefing: %s", {"off": "off", "advisory": "on (once per day at startup, advisory only)",
                                    "veto": "on, VETO mode (briefing can block new trades; shadow-tracked)"}[s.news_mode])
    log.info("Live-tick exits: stop-loss=%s target=%s",
             f"{s.stop_loss_pct}%" if s.stop_loss_pct else "off",
             f"{s.target_pct}%" if s.target_pct else "off")
    log.info("=" * 60)

    if not s.paper_trading and "--yes" not in sys.argv:
        if input("LIVE mode will place REAL orders. Type 'LIVE' to continue: ").strip() != "LIVE":
            sys.exit("Aborted.")

    bot = Bot(s)
    if s.news_briefing:
        # Background thread: trading starts right away; the briefing is advisory only
        from news import run_briefing

        def briefing():
            analysis = run_briefing(bot.kite, s, bot.instruments)
            if s.news_mode == "veto":
                bot.set_news_vetoes(analysis)

        threading.Thread(target=briefing, daemon=True, name="news-briefing").start()
    try:
        bot.run()
    except KeyboardInterrupt:
        if bot.feed:
            bot.feed.stop()
        open_pos = {k: v for k, v in bot.broker.positions.items() if v["qty"]}
        log.info("Stopped by user. Open positions: %s", open_pos or "none")
        if open_pos and bot.intraday:
            log.warning("MIS positions are still open - they will be auto squared off by "
                        "Zerodha near market close (with a charge) if you don't close them.")


if __name__ == "__main__":
    main()
