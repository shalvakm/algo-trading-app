"""Shadow tracking of trades blocked by the news veto.

When the news veto blocks an entry, we follow the trade that WOULD have happened, using
the bot's exit rules (stop-loss/target from candle high/low, opposite signal, square-off),
and log its result to logs/news_vetoes.csv. After a few weeks this shows whether the
vetoes saved money (shadow P&L negative) or cost good trades (shadow P&L positive).

    python shadow.py      # summary of logs/news_vetoes.csv
"""
import csv
import json
import logging
import threading
from datetime import datetime

from config import IST, LOG_DIR, STATE_DIR, Settings
from strategy import exit_levels

log = logging.getLogger("shadow")
VETOES_CSV = LOG_DIR / "news_vetoes.csv"
OPEN_FILE = STATE_DIR / "shadow_open.json"
FIELDS = ["date", "symbol", "side", "qty", "entry_time", "entry", "exit_time", "exit",
          "exit_reason", "return_pct", "pnl_rs", "veto_reason"]


class ShadowBook:
    def __init__(self, settings: Settings):
        self.s = settings
        self.open: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not OPEN_FILE.exists():
            return
        data = json.loads(OPEN_FILE.read_text())
        if data.get("date") == datetime.now(IST).date().isoformat():
            self.open = data.get("open", {})  # same-day restart: keep following them
            if self.open:
                log.info("Shadow: resumed %d vetoed trade(s): %s", len(self.open), sorted(self.open))

    def _save(self) -> None:
        OPEN_FILE.write_text(json.dumps({"date": datetime.now(IST).date().isoformat(), "open": self.open}, indent=2))

    def start(self, sym: str, direction: int, price: float, qty: int, when: datetime, reason: str) -> None:
        with self._lock:
            if sym in self.open or qty <= 0:
                return
            self.open[sym] = {"direction": direction, "entry": price, "qty": qty,
                              "entry_time": when.isoformat(timespec="seconds"), "reason": reason}
            self._save()
        log.info("Shadow: tracking vetoed %s %s x%d @ %.2f", "LONG" if direction > 0 else "SHORT", sym, qty, price)

    def on_candle(self, sym: str, candle, signal: int, when: datetime) -> None:
        """Check a shadow trade's exits against the latest closed candle (stop first, like the backtest)."""
        with self._lock:
            pos = self.open.get(sym)
            if not pos:
                return
            d = pos["direction"]
            stop, target = exit_levels(pos["entry"], d, self.s.stop_loss_pct, self.s.target_pct)
            hi, lo = float(candle["high"]), float(candle["low"])
            if stop is not None and ((d > 0 and lo <= stop) or (d < 0 and hi >= stop)):
                self._close(sym, stop, when, "stop-loss")
            elif target is not None and ((d > 0 and hi >= target) or (d < 0 and lo <= target)):
                self._close(sym, target, when, "target")
            elif signal == -d:
                self._close(sym, float(candle["close"]), when, self.s.strategy.reason(signal))

    def square_off(self, prices: dict[str, float], when: datetime) -> None:
        with self._lock:
            for sym in list(self.open):
                if sym in prices:
                    self._close(sym, prices[sym], when, "square-off")

    def _close(self, sym: str, price: float, when: datetime, reason: str) -> None:
        pos = self.open.pop(sym)
        d, entry, qty = pos["direction"], pos["entry"], pos["qty"]
        ret = d * (price / entry - 1) * 100
        pnl = d * (price - entry) * qty
        row = {"date": when.date().isoformat(), "symbol": sym, "side": "LONG" if d > 0 else "SHORT", "qty": qty,
               "entry_time": pos["entry_time"], "entry": f"{entry:.2f}",
               "exit_time": when.isoformat(timespec="seconds"), "exit": f"{price:.2f}", "exit_reason": reason,
               "return_pct": f"{ret:.3f}", "pnl_rs": f"{pnl:.2f}", "veto_reason": pos["reason"]}
        new_file = not VETOES_CSV.exists()
        with VETOES_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        self._save()
        verdict = "veto SAVED" if pnl < 0 else "veto COST" if pnl > 0 else "no difference,"
        log.info("Shadow: vetoed %s %s would have closed @ %.2f (%s): %+.2f -> %s Rs %.2f",
                 row["side"], sym, price, reason, pnl, verdict, abs(pnl))


def summary() -> None:
    if not VETOES_CSV.exists():
        print("No vetoed trades recorded yet (logs/news_vetoes.csv doesn't exist).")
        return
    rows = list(csv.DictReader(VETOES_CSV.open()))
    if not rows:
        print("No vetoed trades recorded yet.")
        return
    pnl = [float(r["pnl_rs"]) for r in rows]
    total = sum(pnl)
    print(f"Vetoed trades: {len(rows)} over {len({r['date'] for r in rows})} day(s)")
    print(f"  would have won: {sum(p > 0 for p in pnl)} | lost: {sum(p < 0 for p in pnl)}")
    print(f"  their total P&L: Rs {total:+.2f} (gross, like trades.csv)")
    print("  => the news veto " + ("SAVED you" if total < 0 else "COST you" if total > 0 else "made no difference:")
          + f" Rs {abs(total):.2f}")
    by_rule = {}
    for r, p in zip(rows, pnl):
        rule = "event (results/major news)" if r["veto_reason"].startswith("news event") else "outlook"
        n, t = by_rule.get(rule, (0, 0.0))
        by_rule[rule] = (n + 1, t + p)
    for rule, (n, t) in by_rule.items():
        print(f"  {rule}: {n} trades, would-have P&L Rs {t:+.2f}")


if __name__ == "__main__":
    summary()
