"""Entry/exit signal logic for every supported strategy.

Pure functions on DataFrames, shared by the backtester and the live loop, so
what you backtest is exactly what you trade.

Every strategy adds a `signal` column: BUY (+1) on the candle that calls for a long
(or covering a short), SELL (-1) on the candle that calls for exiting a long (or
shorting, if enabled), HOLD (0) otherwise. Signals fire once, on the candle where the
condition first becomes true, never on every candle while it stays true.

Strategies (STRATEGY in .env):
  ma_cross    fast MA crosses above / below slow MA (trend following)
  macd        MACD line crosses above / below its signal line (momentum)
  rsi         RSI recovers above OVERSOLD / falls back below OVERBOUGHT (mean reversion)
  bollinger   close recovers above the lower band / falls back below the upper band (mean reversion)
  supertrend  Supertrend flips up / down (ATR trend following)
  donchian    close breaks above the highest high / below the lowest low of the last N candles (breakout)
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

BUY = 1
SELL = -1
HOLD = 0


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "ma_cross"
    # ma_cross
    fast: int = 9
    slow: int = 21
    ma_type: str = "EMA"
    # rsi
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    # macd
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # bollinger
    bb_period: int = 20
    bb_std: float = 2.0
    # supertrend
    st_period: int = 10
    st_multiplier: float = 3.0
    # donchian
    donchian_period: int = 20

    def validate(self) -> None:
        if self.name not in STRATEGIES:
            raise ValueError(f"STRATEGY must be one of {list(STRATEGIES)}")
        if self.ma_type not in ("SMA", "EMA"):
            raise ValueError("MA_TYPE must be SMA or EMA")
        if self.name == "ma_cross" and self.fast >= self.slow:
            raise ValueError("FAST_PERIOD must be smaller than SLOW_PERIOD")
        if self.name == "rsi":
            if self.rsi_period < 2:
                raise ValueError("RSI_PERIOD must be >= 2")
            if not 0 < self.rsi_oversold < self.rsi_overbought < 100:
                raise ValueError("RSI levels need 0 < RSI_OVERSOLD < RSI_OVERBOUGHT < 100")
        if self.name == "macd":
            if min(self.macd_fast, self.macd_signal) < 1 or self.macd_fast >= self.macd_slow:
                raise ValueError("MACD needs 1 <= MACD_FAST < MACD_SLOW and MACD_SIGNAL >= 1")
        if self.name == "bollinger" and (self.bb_period < 2 or self.bb_std <= 0):
            raise ValueError("BOLLINGER needs BB_PERIOD >= 2 and BB_STD > 0")
        if self.name == "supertrend" and (self.st_period < 1 or self.st_multiplier <= 0):
            raise ValueError("SUPERTREND needs SUPERTREND_PERIOD >= 1 and SUPERTREND_MULTIPLIER > 0")
        if self.name == "donchian" and self.donchian_period < 2:
            raise ValueError("DONCHIAN_PERIOD must be >= 2")

    @property
    def label(self) -> str:
        return {
            "ma_cross": f"{self.ma_type} {self.fast}/{self.slow} crossover",
            "macd": f"MACD {self.macd_fast}/{self.macd_slow}/{self.macd_signal}",
            "rsi": f"RSI({self.rsi_period}) {self.rsi_oversold:g}/{self.rsi_overbought:g}",
            "bollinger": f"Bollinger({self.bb_period}, {self.bb_std:g}sd)",
            "supertrend": f"Supertrend({self.st_period}, {self.st_multiplier:g})",
            "donchian": f"Donchian({self.donchian_period}) breakout",
        }[self.name]

    @property
    def description(self) -> str:
        """One line for the news briefing prompt."""
        return {
            "ma_cross": "a moving-average crossover (trend following)",
            "macd": "a MACD signal-line crossover (momentum)",
            "rsi": "an RSI oversold/overbought reversal (mean reversion)",
            "bollinger": "a Bollinger Band reversal (mean reversion)",
            "supertrend": "a Supertrend (ATR-based trend following)",
            "donchian": "a Donchian channel breakout",
        }[self.name]

    def min_candles(self) -> int:
        """History needed before the latest signal is reliable."""
        if self.name == "ma_cross":
            return min_candles_needed(self.slow, self.ma_type)
        if self.name == "macd":
            return (self.macd_slow + self.macd_signal) * 3 + 2
        if self.name == "rsi":
            return self.rsi_period * 4 + 2  # Wilder smoothing converges slowly
        if self.name == "bollinger":
            return self.bb_period + 2
        if self.name == "supertrend":
            return self.st_period * 4 + 2
        return self.donchian_period + 2  # donchian

    def reason(self, signal: int) -> str:
        """Human-readable name of a BUY/SELL signal, for logs and trade records."""
        up = signal > 0
        return {
            "ma_cross": "golden cross" if up else "death cross",
            "macd": "MACD bullish cross" if up else "MACD bearish cross",
            "rsi": "RSI oversold reversal" if up else "RSI overbought reversal",
            "bollinger": "lower band reversal" if up else "upper band reversal",
            "supertrend": "Supertrend flip up" if up else "Supertrend flip down",
            "donchian": "channel breakout up" if up else "channel breakout down",
        }[self.name]

    def describe(self, row) -> str:
        """Indicator values on one candle, for the per-candle log line."""
        cols = INDICATOR_COLUMNS[self.name]
        return " ".join(f"{c}={row[c]:.2f}" for c in cols)


# ---------- indicators ----------

def moving_average(series: pd.Series, period: int, ma_type: str) -> pd.Series:
    if ma_type == "EMA":
        return series.ewm(span=period, adjust=False, min_periods=period).mean()
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (0-100)."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    out = 100 - 100 / (1 + gain / loss)
    out[(loss == 0) & gain.notna()] = 100.0  # only gains in the window
    return out


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def supertrend(df: pd.DataFrame, period: int, multiplier: float) -> tuple[pd.Series, pd.Series]:
    """Returns (supertrend line, direction +1 up / -1 down / NaN before enough history)."""
    a = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    upper, lower = hl2 + multiplier * a, hl2 - multiplier * a
    line = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(direction[i - 1]):
            direction[i] = 1.0 if close[i] >= hl2[i] else -1.0
        else:
            # bands only ratchet in the trend's favour unless the previous close broke them
            if not (lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]):
                lower[i] = lower[i - 1]
            if not (upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]):
                upper[i] = upper[i - 1]
            if direction[i - 1] < 0 and close[i] > upper[i]:
                direction[i] = 1.0
            elif direction[i - 1] > 0 and close[i] < lower[i]:
                direction[i] = -1.0
            else:
                direction[i] = direction[i - 1]
        line[i] = lower[i] if direction[i] > 0 else upper[i]
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


# ---------- signal helpers ----------

def _crosses(a: pd.Series, b) -> tuple[pd.Series, pd.Series]:
    """(crossed above, crossed below) masks: the candle where `a` moves to the other side of `b`.

    Both candles must have valid values, so warm-up NaNs never produce a signal.
    """
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    valid = a.notna() & b.notna()
    both_valid = valid & valid.shift(1, fill_value=False)
    above = a > b
    prev_above = above.shift(1, fill_value=False)
    below = a < b
    prev_below = below.shift(1, fill_value=False)
    return both_valid & above & ~prev_above, both_valid & below & ~prev_below


def _set_signals(out: pd.DataFrame, buy: pd.Series, sell: pd.Series) -> pd.DataFrame:
    out["signal"] = HOLD
    out.loc[buy, "signal"] = BUY
    out.loc[sell & ~buy, "signal"] = SELL
    return out


# ---------- strategies ----------

def _ma_cross(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["fast_ma"] = moving_average(out["close"], c.fast, c.ma_type)
    out["slow_ma"] = moving_average(out["close"], c.slow, c.ma_type)
    # fast == slow is "not above": a cross down needs fast to go from above to not-above
    above = out["fast_ma"] > out["slow_ma"]
    valid = out["fast_ma"].notna() & out["slow_ma"].notna()
    both_valid = valid & valid.shift(1, fill_value=False)
    prev_above = above.shift(1, fill_value=False)
    return _set_signals(out, both_valid & above & ~prev_above, both_valid & ~above & prev_above)


def _macd(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    fast = moving_average(out["close"], c.macd_fast, "EMA")
    slow = moving_average(out["close"], c.macd_slow, "EMA")
    out["macd"] = fast - slow
    out["macd_signal"] = moving_average(out["macd"], c.macd_signal, "EMA")
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    buy, sell = _crosses(out["macd"], out["macd_signal"])
    return _set_signals(out, buy, sell)


def _rsi(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["rsi"] = rsi(out["close"], c.rsi_period)
    buy, _ = _crosses(out["rsi"], c.rsi_oversold)     # back up out of oversold
    _, sell = _crosses(out["rsi"], c.rsi_overbought)  # back down out of overbought
    return _set_signals(out, buy, sell)


def _bollinger(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    mid = out["close"].rolling(c.bb_period).mean()
    sd = out["close"].rolling(c.bb_period).std(ddof=0)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = mid, mid + c.bb_std * sd, mid - c.bb_std * sd
    buy, _ = _crosses(out["close"], out["bb_lower"])   # closes back inside from below
    _, sell = _crosses(out["close"], out["bb_upper"])  # closes back inside from above
    return _set_signals(out, buy, sell)


def _supertrend(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["supertrend"], out["st_dir"] = supertrend(out, c.st_period, c.st_multiplier)
    prev = out["st_dir"].shift(1)
    return _set_signals(out, (out["st_dir"] == 1) & (prev == -1), (out["st_dir"] == -1) & (prev == 1))


def _donchian(df: pd.DataFrame, c: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    # channel of the PREVIOUS N candles, so the current close can break it
    out["dc_upper"] = out["high"].rolling(c.donchian_period).max().shift(1)
    out["dc_lower"] = out["low"].rolling(c.donchian_period).min().shift(1)
    up = out["close"] > out["dc_upper"]
    down = out["close"] < out["dc_lower"]
    valid = out["dc_upper"].notna() & out["dc_upper"].shift(1).notna()
    return _set_signals(out, valid & up & ~up.shift(1, fill_value=False),
                        valid & down & ~down.shift(1, fill_value=False))


STRATEGIES = {
    "ma_cross": _ma_cross,
    "macd": _macd,
    "rsi": _rsi,
    "bollinger": _bollinger,
    "supertrend": _supertrend,
    "donchian": _donchian,
}

INDICATOR_COLUMNS = {
    "ma_cross": ["fast_ma", "slow_ma"],
    "macd": ["macd", "macd_signal"],
    "rsi": ["rsi"],
    "bollinger": ["bb_lower", "bb_mid", "bb_upper"],
    "supertrend": ["supertrend", "st_dir"],
    "donchian": ["dc_lower", "dc_upper"],
}


def add_signals(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Adds the strategy's indicator columns and a `signal` column."""
    return STRATEGIES[cfg.name](df, cfg)


# ---------- position / exit rules (shared by every strategy) ----------

def target_position(signal: int, current: int, allow_short: bool) -> int:
    """Desired position (+1 long, 0 flat, -1 short) after seeing a signal."""
    if signal == BUY:
        return 1
    if signal == SELL:
        return -1 if allow_short else 0
    return current


def min_candles_needed(slow: int, ma_type: str) -> int:
    # EMAs need extra history to converge to stable values
    return slow * 3 + 2 if ma_type == "EMA" else slow + 2


def exit_levels(entry: float, direction: int, stop_loss_pct: float,
                target_pct: float) -> tuple[float | None, float | None]:
    """Stop-loss and target prices for a position (None = disabled)."""
    sl, tp = stop_loss_pct / 100, target_pct / 100
    stop = entry * (1 - direction * sl) if sl > 0 else None
    target = entry * (1 + direction * tp) if tp > 0 else None
    return stop, target


def exit_reason(price: float, direction: int, stop: float | None, target: float | None) -> str | None:
    """'stop-loss' / 'target' if `price` has hit either level, else None."""
    if direction == 0:
        return None
    if stop is not None and (price - stop) * direction <= 0:
        return "stop-loss"
    if target is not None and (price - target) * direction >= 0:
        return "target"
    return None


def market_trend(index_df: pd.DataFrame, period: int) -> pd.Series:
    """+1 when the index closes above its EMA(period) (market up), -1 below (market down).

    NaN until there's enough history, which blocks entries rather than guessing.
    """
    ema = moving_average(index_df["close"], period, "EMA")
    trend = (index_df["close"] > ema).map({True: 1.0, False: -1.0})
    trend[ema.isna()] = float("nan")
    return trend


def market_allows(target: int, current: int, market_dir) -> bool:
    """Exits and holds are always allowed; a new position must agree with the market."""
    return target in (0, current) or market_dir == target
