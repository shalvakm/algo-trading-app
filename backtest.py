"""Backtest a strategy on Kite historical data (or a local CSV).

Examples:
    python backtest.py --symbol RELIANCE --days 180
    python backtest.py --symbol INFY --interval 5minute --fast 20 --slow 50 --days 90
    python backtest.py --symbol INFY --strategy rsi --rsi-oversold 25 --rsi-overbought 75
    python backtest.py --symbol INFY --strategy all  # compare every strategy on the same data
    python backtest.py --csv mydata.csv          # offline, no login needed

Signals are computed on a candle's close and filled at the NEXT candle's open,
which is what the live bot does. Costs are applied per side.

Stop-loss / target (which the live bot triggers from WebSocket ticks) are checked
against each candle's high/low. If both are hit in the same candle we assume the
stop-loss came first (conservative). A gap through a level fills at the open.

Position size uses the same rules as the live bot (RISK_PER_TRADE / QUANTITY /
MAX_CAPITAL_PER_TRADE), limited by --capital (full share value, no MIS leverage).
"""
import argparse
import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd

from config import IST, LOG_DIR, load_settings
from sizing import position_size
from strategy import (STRATEGIES, StrategyConfig, add_signals, exit_levels, market_allows,
                      market_trend, target_position)


def run_backtest(df: pd.DataFrame, strategy: StrategyConfig, allow_short: bool,
                 intraday: bool, square_off_time, cost_pct: float,
                 stop_loss_pct: float = 0.0, target_pct: float = 0.0,
                 size_fn=None, capital: float = 0.0,
                 no_entries_after=None, market_dir: pd.Series | None = None) -> tuple[pd.DataFrame, dict]:
    df = add_signals(df, strategy)
    # market direction (+1/-1) at each candle's close; missing -> NaN -> blocks entries
    market = market_dir.reindex(df.index) if market_dir is not None else None
    blocked = 0
    cost = cost_pct / 100
    trades = []
    position, entry_price, entry_time, qty = 0, 0.0, None, 0
    total_pnl = gross_pnl = total_costs = 0.0
    skipped = 0
    pending = None  # target position to execute at next open
    equity, peak, max_dd = 1.0, 1.0, 0.0

    def close_trade(price, when, reason):
        nonlocal position, equity, peak, max_dd, total_pnl, gross_pnl, total_costs
        ret = position * (price / entry_price - 1) - 2 * cost
        gross = position * (price - entry_price) * qty
        costs = cost * (entry_price + price) * qty
        gross_pnl += gross
        total_costs += costs
        total_pnl += gross - costs
        equity *= 1 + ret
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        trades.append({
            "entry_time": entry_time, "exit_time": when,
            "side": "LONG" if position > 0 else "SHORT",
            "qty": qty, "entry": round(entry_price, 2), "exit": round(price, 2),
            "return_pct": round(ret * 100, 3), "pnl_rs": round(gross - costs, 2), "reason": reason,
        })
        position = 0

    for ts, bar in df.iterrows():
        after_cutoff = intraday and ts.time() >= square_off_time

        # 1. execute pending order at this candle's open
        if pending and pending != position and no_entries_after and ts.time() >= no_entries_after:
            pending = 0  # too late in the day to open a new position; still allowed to close
        if pending is not None and pending != position and not after_cutoff:
            if position != 0:
                close_trade(bar["open"], ts, "signal")
            if pending != 0:
                q = size_fn(bar["open"]) if size_fn else 1
                if capital > 0:
                    q = min(q, math.floor((capital + total_pnl) / bar["open"]))
                if q > 0:
                    position, entry_price, entry_time, qty = pending, bar["open"], ts, q
                else:
                    skipped += 1
        pending = None

        # 2. intraday square-off
        if after_cutoff:
            if position != 0:
                close_trade(bar["open"], ts, "square-off")
            continue

        # 3. stop-loss / target inside this candle
        if position != 0:
            stop, target = exit_levels(entry_price, position, stop_loss_pct, target_pct)
            if position > 0:
                stop_hit = stop is not None and bar["low"] <= stop
                target_hit = target is not None and bar["high"] >= target
                stop_fill = min(stop, bar["open"]) if stop_hit else None
                target_fill = max(target, bar["open"]) if target_hit else None
            else:
                stop_hit = stop is not None and bar["high"] >= stop
                target_hit = target is not None and bar["low"] <= target
                stop_fill = max(stop, bar["open"]) if stop_hit else None
                target_fill = min(target, bar["open"]) if target_hit else None
            if stop_hit:
                close_trade(stop_fill, ts, "stop-loss")
            elif target_hit:
                close_trade(target_fill, ts, "target")

        # 4. new signal on this candle's close -> act at next open
        if bar["signal"] != 0:
            pending = target_position(int(bar["signal"]), position, allow_short)
            if market is not None and not market_allows(pending, position, market.get(ts)):
                pending = 0
                blocked += 1

    if position != 0:
        close_trade(df["close"].iloc[-1], df.index[-1], "end of data")

    trades_df = pd.DataFrame(trades)
    wins = (trades_df["return_pct"] > 0).sum() if len(trades_df) else 0
    stats = {
        "candles": len(df),
        "period": f"{df.index[0]} -> {df.index[-1]}",
        "trades": len(trades_df),
        "win_rate_pct": round(100 * wins / len(trades_df), 1) if len(trades_df) else 0.0,
        "avg_trade_pct": round(trades_df["return_pct"].mean(), 3) if len(trades_df) else 0.0,
        "strategy_return_pct": round((equity - 1) * 100, 2),
        "buy_and_hold_pct": round((df["close"].iloc[-1] / df["open"].iloc[0] - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_shares_per_trade": round(trades_df["qty"].mean(), 1) if len(trades_df) else 0,
        "gross_pnl_rs": round(gross_pnl, 2),
        "costs_rs": round(total_costs, 2),
        "net_pnl_rs": round(total_pnl, 2),
        "entries_skipped_no_funds": skipped,
        "entries_blocked_by_market": blocked,
    }
    return trades_df, stats


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    return df.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]


def main():
    s = load_settings()
    p = argparse.ArgumentParser(description="Backtest a trading strategy")
    p.add_argument("--symbol", default=s.symbols[0])
    p.add_argument("--interval", default=s.interval)
    p.add_argument("--days", type=int, default=180)
    st = s.strategy
    p.add_argument("--strategy", default=st.name, choices=[*STRATEGIES, "all"],
                   help="'all' backtests every strategy on the same data and prints a comparison")
    g = p.add_argument_group("strategy parameters (defaults from .env)")
    g.add_argument("--fast", type=int, default=st.fast, help="ma_cross fast period")
    g.add_argument("--slow", type=int, default=st.slow, help="ma_cross slow period")
    g.add_argument("--ma-type", default=st.ma_type, choices=["SMA", "EMA"])
    g.add_argument("--rsi-period", type=int, default=st.rsi_period)
    g.add_argument("--rsi-oversold", type=float, default=st.rsi_oversold)
    g.add_argument("--rsi-overbought", type=float, default=st.rsi_overbought)
    g.add_argument("--macd-fast", type=int, default=st.macd_fast)
    g.add_argument("--macd-slow", type=int, default=st.macd_slow)
    g.add_argument("--macd-signal", type=int, default=st.macd_signal)
    g.add_argument("--bb-period", type=int, default=st.bb_period)
    g.add_argument("--bb-std", type=float, default=st.bb_std)
    g.add_argument("--st-period", type=int, default=st.st_period, help="supertrend ATR period")
    g.add_argument("--st-multiplier", type=float, default=st.st_multiplier)
    g.add_argument("--donchian-period", type=int, default=st.donchian_period)
    p.add_argument("--product", default=s.product, choices=["MIS", "CNC"])
    p.add_argument("--allow-short", action=argparse.BooleanOptionalAction, default=s.allow_short,
                   help="also short on SELL signals (default: ALLOW_SHORT from .env)")
    p.add_argument("--stop-loss-pct", type=float, default=s.stop_loss_pct, help="0 = off")
    p.add_argument("--target-pct", type=float, default=s.target_pct, help="0 = off")
    p.add_argument("--risk-per-trade", type=float, default=s.risk_per_trade,
                   help="rupees lost if the stop-loss is hit (0 = fixed QUANTITY)")
    p.add_argument("--capital", type=float, default=s.paper_funds, help="starting funds in rupees")
    p.add_argument("--cost-pct", type=float, default=0.05,
                   help="approx. brokerage+taxes+slippage per side, in percent")
    p.add_argument("--market-filter", action=argparse.BooleanOptionalAction, default=s.market_filter,
                   help=f"only enter in the direction of {s.market_index} vs its EMA")
    p.add_argument("--market-ma", type=int, default=s.market_ma_period)
    p.add_argument("--csv", help="backtest a local OHLC CSV instead of fetching from Kite")
    a = p.parse_args()

    base = StrategyConfig(
        name=st.name, fast=a.fast, slow=a.slow, ma_type=a.ma_type,
        rsi_period=a.rsi_period, rsi_oversold=a.rsi_oversold, rsi_overbought=a.rsi_overbought,
        macd_fast=a.macd_fast, macd_slow=a.macd_slow, macd_signal=a.macd_signal,
        bb_period=a.bb_period, bb_std=a.bb_std, st_period=a.st_period,
        st_multiplier=a.st_multiplier, donchian_period=a.donchian_period)
    names = list(STRATEGIES) if a.strategy == "all" else [a.strategy]
    strategies = [replace(base, name=n) for n in names]
    for cfg in strategies:
        try:
            cfg.validate()
        except ValueError as e:
            raise SystemExit(f"{cfg.name}: {e}")

    if a.csv:
        df = load_csv(a.csv)
        label = a.csv
    else:
        from auth import get_kite
        from data import fetch_history, resolve_instruments
        kite = get_kite(s)
        inst = resolve_instruments(kite, replace(s, symbols=[a.symbol.upper()]))
        token = inst[a.symbol.upper()]["instrument_token"]
        end = datetime.now(IST)
        df = fetch_history(kite, token, a.interval, end - timedelta(days=a.days), end)
        label = f"{a.symbol.upper()} {a.interval}"

    if df.empty:
        raise SystemExit("No data returned.")

    market_dir = None
    if a.market_filter:
        if a.csv:
            print("Note: --market-filter needs Kite index data; ignored for --csv backtests.")
        else:
            from data import resolve_index_token
            idx = fetch_history(kite, resolve_index_token(kite, s.exchange, s.market_index),
                                a.interval, end - timedelta(days=a.days), end)
            market_dir = market_trend(idx, a.market_ma)

    intraday = a.product == "MIS" and a.interval != "day"
    sizing = replace(s, stop_loss_pct=a.stop_loss_pct, risk_per_trade=a.risk_per_trade)
    if sizing.risk_per_trade > 0 and sizing.stop_loss_pct <= 0:
        print("Note: risk-based sizing needs a stop-loss; using fixed QUANTITY instead.")
        sizing = replace(sizing, risk_per_trade=0)
    kwargs = dict(allow_short=a.allow_short, intraday=intraday, square_off_time=s.square_off_time,
                  cost_pct=a.cost_pct, stop_loss_pct=a.stop_loss_pct, target_pct=a.target_pct,
                  size_fn=lambda price: position_size(price, sizing), capital=a.capital,
                  no_entries_after=s.no_new_entries_after if a.interval != "day" else None,
                  market_dir=market_dir)
    setup = (f"{'intraday' if intraday else 'positional'}"
             f"{' | long/short' if a.allow_short else ' | long only'}"
             f" | SL {f'{a.stop_loss_pct}%' if a.stop_loss_pct else 'off'}"
             f" TP {f'{a.target_pct}%' if a.target_pct else 'off'}")
    sizing_line = ("  sizing: " + (f"risk Rs {sizing.risk_per_trade:.0f}/trade" if sizing.risk_per_trade
                                   else f"{sizing.quantity} shares/trade")
                   + (f", max Rs {sizing.max_capital_per_trade:.0f}/position" if sizing.max_capital_per_trade else "")
                   + f", capital Rs {a.capital:.0f}"
                   + (f" | market filter: {s.market_index} vs EMA{a.market_ma}" if market_dir is not None else ""))
    name = (a.symbol if not a.csv else "csv").upper()

    if len(strategies) > 1:
        rows = []
        for cfg in strategies:
            trades, stats = run_backtest(df, cfg, **kwargs)
            rows.append({"strategy": cfg.label, "trades": stats["trades"],
                         "win_%": stats["win_rate_pct"], "avg_trade_%": stats["avg_trade_pct"],
                         "return_%": stats["strategy_return_pct"], "max_dd_%": stats["max_drawdown_pct"],
                         "net_pnl_rs": stats["net_pnl_rs"]})
            if len(trades):
                trades.to_csv(LOG_DIR / f"backtest_{name}_{a.interval}_{cfg.name}.csv", index=False)
        table = pd.DataFrame(rows).sort_values("net_pnl_rs", ascending=False)
        print(f"\n=== Strategy comparison: {label} | {setup} ===")
        print(sizing_line)
        print(f"  period: {df.index[0]} -> {df.index[-1]} ({len(df)} candles), buy & hold "
              f"{round((df['close'].iloc[-1] / df['open'].iloc[0] - 1) * 100, 2)}%\n")
        print(table.to_string(index=False))
        print(f"\n  Per-strategy trades saved to {LOG_DIR}/backtest_{name}_{a.interval}_<strategy>.csv")
        print("  Past results on one stock and period don't predict the future - check several before choosing.")
        return

    cfg = strategies[0]
    trades, stats = run_backtest(df, cfg, **kwargs)
    print(f"\n=== Backtest: {label} | {cfg.label} | {setup} ===")
    print(sizing_line)
    for k, v in stats.items():
        print(f"  {k:<22} {v}")

    if len(trades):
        out = LOG_DIR / f"backtest_{name}_{a.interval}.csv"
        trades.to_csv(out, index=False)
        print(f"\n  Trades saved to {out}")
        print("\nLast 5 trades:")
        print(trades.tail().to_string(index=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
