"""Loads all settings from .env so nothing sensitive is hardcoded."""
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

VALID_INTERVALS = {
    "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
    "15minute": 15, "30minute": 30, "60minute": 60, "day": 375,
}


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y")


def _time(name: str, default: str) -> time:
    h, m = os.getenv(name, default).split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    symbols: list[str]
    symbol_mode: str
    scan_top: int
    scan_min_price: float
    protected_symbols: frozenset[str]
    exchange: str
    interval: str
    fast_period: int
    slow_period: int
    ma_type: str
    allow_short: bool
    market_filter: bool
    market_index: str
    market_ma_period: int
    market_neutral_pct: float
    product: str
    quantity: int
    risk_per_trade: float
    max_capital_per_trade: float
    max_total_capital: float
    max_daily_loss: float
    paper_funds: float
    limit_buffer_pct: float
    square_off_time: time
    no_new_entries_after: time | None
    join_young_trends: bool
    join_max_age: int
    stop_loss_pct: float
    target_pct: float
    stop_mode: str
    atr_mult: float
    atr_period: int
    swing_bars: int
    target_r: float
    stop_min_pct: float
    stop_max_pct: float
    paper_trading: bool
    max_trades_per_day: int
    news_mode: str
    keep_days: int

    @property
    def news_briefing(self) -> bool:
        return self.news_mode != "off"

    @property
    def tick_exits_enabled(self) -> bool:
        return self.stop_mode != "fixed" or self.stop_loss_pct > 0 or self.target_pct > 0

    @property
    def interval_minutes(self) -> int:
        return VALID_INTERVALS[self.interval]


def load_settings() -> Settings:
    s = Settings(
        api_key=os.getenv("KITE_API_KEY", ""),
        api_secret=os.getenv("KITE_API_SECRET", ""),
        symbols=[x.strip().upper() for x in os.getenv("SYMBOLS", "RELIANCE").split(",") if x.strip()],
        symbol_mode=os.getenv("SYMBOL_MODE", "fixed").strip().lower(),
        scan_top=int(os.getenv("SCAN_TOP", "20")),
        scan_min_price=float(os.getenv("SCAN_MIN_PRICE", "50")),
        protected_symbols=frozenset(x.strip().upper() for x in os.getenv("PROTECTED_SYMBOLS", "").split(",") if x.strip()),
        exchange=os.getenv("EXCHANGE", "NSE").upper(),
        interval=os.getenv("INTERVAL", "15minute"),
        fast_period=int(os.getenv("FAST_PERIOD", "9")),
        slow_period=int(os.getenv("SLOW_PERIOD", "21")),
        ma_type=os.getenv("MA_TYPE", "EMA").upper(),
        allow_short=_bool("ALLOW_SHORT", "false"),
        market_filter=_bool("MARKET_FILTER", "false"),
        market_index=os.getenv("MARKET_INDEX", "NIFTY 50").strip().upper(),
        market_ma_period=int(os.getenv("MARKET_MA_PERIOD", "50")),
        market_neutral_pct=float(os.getenv("MARKET_NEUTRAL_PCT", "0")),
        product=os.getenv("PRODUCT", "MIS").upper(),
        quantity=int(os.getenv("QUANTITY", "1")),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0")),
        max_capital_per_trade=float(os.getenv("MAX_CAPITAL_PER_TRADE", "0")),
        max_total_capital=float(os.getenv("MAX_TOTAL_CAPITAL", "0")),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "0")),
        paper_funds=float(os.getenv("PAPER_FUNDS", "100000")),
        limit_buffer_pct=float(os.getenv("LIMIT_BUFFER_PCT", "0.1")),
        square_off_time=_time("SQUARE_OFF_TIME", "15:15"),
        join_young_trends=_bool("JOIN_YOUNG_TRENDS", "false"),
        join_max_age=int(os.getenv("JOIN_MAX_AGE", "7")),
        no_new_entries_after=_time("NO_NEW_ENTRIES_AFTER", "") if os.getenv("NO_NEW_ENTRIES_AFTER", "").strip() else None,
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0")),
        target_pct=float(os.getenv("TARGET_PCT", "0")),
        stop_mode=os.getenv("STOP_MODE", "fixed").strip().lower(),
        atr_mult=float(os.getenv("ATR_MULT", "2")),
        atr_period=int(os.getenv("ATR_PERIOD", "14")),
        swing_bars=int(os.getenv("SWING_BARS", "5")),
        target_r=float(os.getenv("TARGET_R", "0")),
        stop_min_pct=float(os.getenv("STOP_MIN_PCT", "0.5")),
        stop_max_pct=float(os.getenv("STOP_MAX_PCT", "5")),
        paper_trading=_bool("PAPER_TRADING", "true"),
        max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
        keep_days=int(os.getenv("KEEP_DAYS", "0")),
        news_mode=(os.getenv("NEWS_MODE", "").strip().lower()
                   or ("advisory" if _bool("NEWS_BRIEFING", "false") else "off")),
    )
    _validate(s)
    return s


def _validate(s: Settings) -> None:
    if s.symbol_mode not in ("fixed", "scan"):
        raise ValueError("SYMBOL_MODE must be fixed or scan")
    if s.symbol_mode == "scan" and not 1 <= s.scan_top <= 50:
        raise ValueError("SCAN_TOP must be between 1 and 50")
    blocked = s.protected_symbols.intersection(s.symbols)
    if blocked:
        raise ValueError(f"SYMBOLS contains protected stocks {sorted(blocked)} - remove them from SYMBOLS")
    if s.interval not in VALID_INTERVALS:
        raise ValueError(f"INTERVAL must be one of {list(VALID_INTERVALS)}")
    if s.fast_period >= s.slow_period:
        raise ValueError("FAST_PERIOD must be smaller than SLOW_PERIOD")
    if s.ma_type not in ("SMA", "EMA"):
        raise ValueError("MA_TYPE must be SMA or EMA")
    if s.product not in ("MIS", "CNC"):
        raise ValueError("PRODUCT must be MIS or CNC")
    if s.allow_short and s.product != "MIS":
        raise ValueError("ALLOW_SHORT requires PRODUCT=MIS (equity delivery can't be shorted)")
    if (s.no_new_entries_after and s.product == "MIS"
            and s.no_new_entries_after >= s.square_off_time):
        raise ValueError("NO_NEW_ENTRIES_AFTER must be earlier than SQUARE_OFF_TIME")
    if not 0 <= s.market_neutral_pct < 5:
        raise ValueError("MARKET_NEUTRAL_PCT must be between 0 and 5 (percent)")
    if s.market_filter and s.market_ma_period < 2:
        raise ValueError("MARKET_MA_PERIOD must be >= 2")
    if s.keep_days < 0:
        raise ValueError("KEEP_DAYS must be >= 0 (0 = keep everything)")
    if s.news_mode not in ("off", "advisory", "veto"):
        raise ValueError("NEWS_MODE must be off, advisory or veto")
    if s.join_young_trends and not 1 <= s.join_max_age <= 50:
        raise ValueError("JOIN_MAX_AGE must be between 1 and 50 candles")
    if s.stop_loss_pct < 0 or s.target_pct < 0:
        raise ValueError("STOP_LOSS_PCT and TARGET_PCT must be >= 0 (0 = disabled)")
    if min(s.risk_per_trade, s.max_capital_per_trade, s.max_total_capital, s.max_daily_loss) < 0:
        raise ValueError("RISK_PER_TRADE, MAX_CAPITAL_PER_TRADE, MAX_TOTAL_CAPITAL and MAX_DAILY_LOSS must be >= 0")
    if s.stop_mode not in ("fixed", "atr", "swing"):
        raise ValueError("STOP_MODE must be fixed, atr or swing")
    if s.stop_mode != "fixed" and not (0 < s.stop_min_pct <= s.stop_max_pct):
        raise ValueError("Need 0 < STOP_MIN_PCT <= STOP_MAX_PCT")
    if s.risk_per_trade > 0 and s.stop_loss_pct <= 0 and s.stop_mode == "fixed":
        raise ValueError("RISK_PER_TRADE needs STOP_LOSS_PCT > 0 (risk is measured to the stop-loss)")
    if s.quantity < 1:
        raise ValueError("QUANTITY must be >= 1")
