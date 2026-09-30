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
