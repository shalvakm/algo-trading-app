"""Moving average crossover signal logic.

Pure functions on DataFrames, shared by the backtester and the live loop, so
what you backtest is exactly what you trade.
"""
import pandas as pd

BUY = 1
SELL = -1
HOLD = 0


def moving_average(series: pd.Series, period: int, ma_type: str) -> pd.Series:
    if ma_type == "EMA":
        return series.ewm(span=period, adjust=False, min_periods=period).mean()
    return series.rolling(period).mean()


def add_signals(df: pd.DataFrame, fast: int, slow: int, ma_type: str) -> pd.DataFrame:
    """Adds fast_ma, slow_ma and signal columns.

    signal = BUY  on the candle where fast crosses above slow (golden cross)
    signal = SELL on the candle where fast crosses below slow (death cross)
    """
    out = df.copy()
    out["fast_ma"] = moving_average(out["close"], fast, ma_type)
    out["slow_ma"] = moving_average(out["close"], slow, ma_type)

    above = out["fast_ma"] > out["slow_ma"]
    valid = out["fast_ma"].notna() & out["slow_ma"].notna()
    prev_above = above.shift(1)
    prev_valid = valid.shift(1, fill_value=False)

    out["signal"] = HOLD
    both_valid = valid & prev_valid
    out.loc[both_valid & above & (prev_above == False), "signal"] = BUY  # noqa: E712
    out.loc[both_valid & ~above & (prev_above == True), "signal"] = SELL  # noqa: E712
    return out


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


def market_trend(index_df: pd.DataFrame, period: int, neutral_pct: float = 0.0) -> pd.Series:
    """+1 when the index closes above its EMA(period) (market up), -1 below (market down).

    With neutral_pct > 0, closes within +/-neutral_pct% of the EMA are 0 (no clear trend), which
    blocks new entries in both directions. NaN until there's enough history (also blocks entries).
    """
    ema = moving_average(index_df["close"], period, "EMA")
    band = neutral_pct / 100
    close = index_df["close"]
    trend = pd.Series(0.0, index=index_df.index)
    trend[close > ema * (1 + band)] = 1.0
    trend[close < ema * (1 - band)] = -1.0
    if band == 0:
        trend[close == ema] = -1.0  # same as before: only strictly above counts as up
    trend[ema.isna()] = float("nan")
    return trend


def market_allows(target: int, current: int, market_dir) -> bool:
    """Exits and holds are always allowed; a new position must agree with the market."""
    return target in (0, current) or market_dir == target


def trend_age(sig_df: pd.DataFrame) -> pd.Series:
    """Candles since the last crossover (0 on the crossover candle itself)."""
    return sig_df.groupby((sig_df["signal"] != 0).cumsum()).cumcount()


def young_trend_joins(sig_df: pd.DataFrame, market: pd.Series | None, max_age: int) -> pd.Series:
    """Direction to JOIN an ongoing trend at each candle (+1/-1, 0 = none).

    A join is allowed on a candle with no fresh crossover when the stock's trend (fast vs slow) started
    1..max_age candles ago and points the same way as the market (if a market series is given).
    At most one join per stock per day (the first qualifying candle).
    """
    trend = (sig_df["fast_ma"] > sig_df["slow_ma"]).map({True: 1, False: -1}).where(sig_df["slow_ma"].notna(), 0)
    age = trend_age(sig_df)
    cand = (sig_df["signal"] == 0) & (age >= 1) & (age <= max_age) & (trend != 0)
    if market is not None:
        cand &= trend == market.reindex(sig_df.index)
    first = cand & (cand.groupby(sig_df.index.date).cumsum() == 1)
    return trend.where(first, 0).astype(int)


# ---------- stops sized to each stock's movement (STOP_MODE) ----------
def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range: the stock's typical candle-to-candle movement."""
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def stop_distance(entry: float, direction: int, mode: str, stop_pct: float, atr_value: float | None = None,
                  swing_low: float | None = None, swing_high: float | None = None, atr_mult: float = 2.0,
                  min_pct: float = 0.5, max_pct: float = 5.0) -> float | None:
    """Rupees per share between entry and the stop-loss (None = no stop).

    fixed: STOP_LOSS_PCT of entry
    atr:   ATR_MULT x the ATR of the signal candle
    swing: just beyond the lowest low (long) / highest high (short) of the last SWING_BARS candles
    atr/swing distances are kept between STOP_MIN_PCT and STOP_MAX_PCT of the entry price.
    """
    if mode == "fixed":
        return entry * stop_pct / 100 if stop_pct > 0 else None
    if mode == "atr":
        dist = atr_mult * atr_value if atr_value and atr_value == atr_value else entry * stop_pct / 100
    else:
        level = swing_low if direction > 0 else swing_high
        dist = (entry - level if direction > 0 else level - entry) * 1.0005 if level and level == level else entry * stop_pct / 100
    return min(max(dist, entry * min_pct / 100), entry * max_pct / 100)


def trade_levels(entry: float, direction: int, dist: float | None, mode: str, target_r: float,
                 target_pct: float) -> tuple[float | None, float | None]:
    """Stop-loss and target prices for a new position. fixed mode uses TARGET_PCT;
    atr/swing use TARGET_R x the stop distance (0 = no target: exit on crossover / stop / square-off)."""
    stop = entry - direction * dist if dist else None
    if mode == "fixed":
        target = entry * (1 + direction * target_pct / 100) if target_pct > 0 else None
    else:
        target = entry + direction * target_r * dist if (target_r > 0 and dist) else None
    return stop, target
