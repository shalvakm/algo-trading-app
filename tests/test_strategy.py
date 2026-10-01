"""Strategy signal tests on synthetic prices.  python -m unittest discover tests"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy import BUY, HOLD, SELL, STRATEGIES, StrategyConfig, add_signals, rsi  # noqa: E402


def candles(close) -> pd.DataFrame:
    close = pd.Series(close, dtype=float)
    idx = pd.date_range("2024-01-01 09:15", periods=len(close), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]).values, "high": close.values + 0.5,
                         "low": close.values - 0.5, "close": close.values, "volume": 1000}, index=idx)


def v_shape(n=60, top=100.0, bottom=70.0):
    """Falls steadily, then rises steadily: every strategy should go SELL-side then BUY-side."""
    return list(np.linspace(top, bottom, n)) + list(np.linspace(bottom, top + 30, n))


def random_walk(n=500, seed=1):
    rng = np.random.default_rng(seed)
    return 100 + np.cumsum(rng.normal(0, 1, n))


class StrategyTests(unittest.TestCase):
    def test_all_strategies_produce_valid_signals(self):
        df = candles(random_walk())
        for name in STRATEGIES:
            with self.subTest(strategy=name):
                cfg = StrategyConfig(name=name)
                cfg.validate()
                out = add_signals(df, cfg)
                self.assertEqual(len(out), len(df))
                self.assertTrue(set(out["signal"].unique()) <= {BUY, SELL, HOLD})
                self.assertGreater((out["signal"] != HOLD).sum(), 0, "random walk should trigger signals")
                cfg.describe(out.iloc[-1])  # indicator columns exist
                self.assertTrue(cfg.reason(BUY) and cfg.reason(SELL))

    def test_no_signals_during_warmup(self):
        df = candles(random_walk())
        for name in STRATEGIES:
            with self.subTest(strategy=name):
                cfg = StrategyConfig(name=name)
                out = add_signals(df.iloc[:5], cfg)
                self.assertTrue((out["signal"] == HOLD).all())

    def test_no_lookahead(self):
        """A signal on candle i must not change when later candles are added."""
        df = candles(random_walk(300, seed=7))
        for name in STRATEGIES:
            with self.subTest(strategy=name):
                cfg = StrategyConfig(name=name)
                full = add_signals(df, cfg)["signal"]
                for cut in (120, 200, 260):
                    part = add_signals(df.iloc[:cut], cfg)["signal"]
                    pd.testing.assert_series_equal(part, full.iloc[:cut])

    def test_trend_strategies_follow_a_v(self):
        df = candles(v_shape())
        for name in ("ma_cross", "macd", "supertrend", "donchian"):
            with self.subTest(strategy=name):
                sig = add_signals(df, StrategyConfig(name=name))["signal"]
                buys = sig[sig == BUY].index
                self.assertTrue(len(buys) > 0)
                self.assertGreaterEqual(buys[-1], df.index[60], "should buy at or after the bottom")

    def test_rsi_buys_when_leaving_oversold(self):
        out = add_signals(candles(v_shape()), StrategyConfig(name="rsi"))
        buy_at = out.index[out["signal"] == BUY]
        self.assertEqual(len(buy_at), 1)
        i = out.index.get_loc(buy_at[0])
        self.assertLessEqual(out["rsi"].iloc[i - 1], 30)
        self.assertGreater(out["rsi"].iloc[i], 30)

    def test_rsi_range(self):
        r = rsi(pd.Series(random_walk()), 14).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())

    def test_bollinger_buy_reenters_band(self):
        prices = [100.0] * 30 + [90.0, 101.0] + [100.0] * 5
        out = add_signals(candles(prices), StrategyConfig(name="bollinger"))
        self.assertEqual(out["signal"].iloc[31], BUY)

    def test_donchian_fires_once_per_breakout(self):
        prices = [100.0] * 25 + [110.0, 111.0, 112.0]
        out = add_signals(candles(prices), StrategyConfig(name="donchian"))
        self.assertEqual(list(out["signal"].iloc[25:]), [BUY, HOLD, HOLD])

    def test_signals_fire_once_not_every_candle(self):
        df = candles(random_walk())
        for name in ("ma_cross", "macd", "supertrend"):
            with self.subTest(strategy=name):
                sig = add_signals(df, StrategyConfig(name=name))["signal"]
                nz = sig[sig != HOLD]
                # trend strategies alternate BUY / SELL
                self.assertTrue((nz.values[1:] != nz.values[:-1]).all())

    def test_ma_cross_matches_original_rule(self):
        df = candles(random_walk())
        out = add_signals(df, StrategyConfig(name="ma_cross", fast=5, slow=20, ma_type="SMA"))
        above = out["fast_ma"] > out["slow_ma"]
        valid = out["fast_ma"].notna() & out["slow_ma"].notna()
        ok = valid & valid.shift(1, fill_value=False)
        expected = np.where(ok & above & ~above.shift(1, fill_value=False), BUY,
                            np.where(ok & ~above & above.shift(1, fill_value=False), SELL, HOLD))
        self.assertTrue((out["signal"].values == expected).all())

    def test_validation(self):
        bad = [StrategyConfig(name="nope"), StrategyConfig(fast=30, slow=20),
               StrategyConfig(name="rsi", rsi_oversold=70, rsi_overbought=30),
               StrategyConfig(name="macd", macd_fast=30), StrategyConfig(name="bollinger", bb_std=0),
               StrategyConfig(name="supertrend", st_multiplier=0), StrategyConfig(name="donchian", donchian_period=1)]
        for cfg in bad:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    cfg.validate()
        # ma_cross periods are only checked when ma_cross is the active strategy
        replace(StrategyConfig(fast=30, slow=20), name="rsi").validate()


if __name__ == "__main__":
    unittest.main()
