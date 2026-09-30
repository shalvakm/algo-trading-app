"""Order placement and position tracking, in paper or live mode.

The bot only manages positions it opened itself (tracked in state/). It never
touches holdings or positions you created manually.
"""
import csv
import json
import logging
import time as _time
from datetime import datetime

from kiteconnect import KiteConnect

from config import IST, LOG_DIR, STATE_DIR, Settings
from data import with_retry
from sizing import fit_to_funds, position_size

log = logging.getLogger(__name__)
TRADES_CSV = LOG_DIR / "trades.csv"
ORDER_FILL_TIMEOUT = 30  # seconds to wait for a limit order to fill before cancelling


def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


class ProtectedSymbolError(RuntimeError):
    pass


class Broker:
    def __init__(self, kite: KiteConnect, settings: Settings, instruments: dict[str, dict]):
        self.kite = kite
        self.s = settings
        self.instruments = instruments
        self.mode = "paper" if settings.paper_trading else "live"
        self.state_file = STATE_DIR / f"positions_{self.mode}.json"
        self.positions: dict[str, dict] = {}  # sym -> {"qty": signed int, "avg_price": float}
        self.trades_today = 0
        self.realized_today = 0.0  # for MAX_DAILY_LOSS
        self._daily_loss_logged = False
        self._load_state()

    # ---------- state ----------
    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text())
        today = datetime.now(IST).date().isoformat()
        if data.get("date") != today:
            self.trades_today, self.realized_today = 0, 0.0
            if self.s.product == "MIS":
                # Intraday positions are squared off by the exchange/broker by end of day
                self.positions = {}
                self._save_state()
                return
        else:
            self.trades_today = data.get("trades_today", 0)
            self.realized_today = data.get("realized_today", 0.0)
        self.positions = {k: v for k, v in data.get("positions", {}).items()
                          if k.upper() not in self.s.protected_symbols}
        open_pos = {k: v for k, v in self.positions.items() if v["qty"]}
        if open_pos:
            log.info("Restored open positions: %s", open_pos)

    def _save_state(self) -> None:
        self.state_file.write_text(json.dumps({
            "date": datetime.now(IST).date().isoformat(),
            "trades_today": self.trades_today,
            "realized_today": round(self.realized_today, 2),
            "positions": self.positions,
        }, indent=2))

    def _guard(self, sym: str) -> None:
        if sym.upper() in self.s.protected_symbols:
            raise ProtectedSymbolError(f"Refusing to trade protected symbol {sym}")

    def position_dir(self, sym: str) -> int:
        qty = self.positions.get(sym, {}).get("qty", 0)
        return (qty > 0) - (qty < 0)

    # ---------- orders ----------
    def ltp(self, sym: str) -> float:
        key = f"{self.s.exchange}:{sym}"
        return float(with_retry(self.kite.ltp, [key])[key]["last_price"])

    def set_target(self, sym: str, target_dir: int, reason: str) -> None:
        """Moves the position in `sym` to target_dir (+1 long / 0 flat / -1 short).

        A reversal (long -> short) is done as close, then a freshly sized open.
        """
        self._guard(sym)
        current_qty = self.positions.get(sym, {}).get("qty", 0)
        if target_dir == self.position_dir(sym):
            return

        if current_qty != 0:
            self._execute(sym, -current_qty, reason, opening=False)
            if self.positions.get(sym, {}).get("qty", 0) != 0:
                log.warning("%s: position not fully closed, not opening a new one", sym)
                return
        if target_dir == 0:
            return
        if target_dir < 0 and not self.s.allow_short:
            log.error("%s: refusing to open a SHORT - ALLOW_SHORT is false (LONG only)", sym)
            return

        if self.trades_today >= self.s.max_trades_per_day:
            log.warning("%s: max trades/day (%d) reached, not opening", sym, self.s.max_trades_per_day)
            return

        if self.daily_loss_reached():
            return

        ltp = self.ltp(sym)
        side = "BUY" if target_dir > 0 else "SELL"
        qty = position_size(ltp, self.s)
        if qty == 0:
            log.warning("%s: 1 share @ %.2f exceeds MAX_CAPITAL_PER_TRADE (%.0f), skipping",
                        sym, ltp, self.s.max_capital_per_trade)
            return
        qty = self._fit_total_capital(sym, qty, ltp)
        if qty == 0:
            return

        available = self.available_funds()
        required = self._required_margin(sym, side, qty, ltp)
        fitted = fit_to_funds(qty, required, available)
        if fitted == 0:
            log.warning("%s: insufficient funds - need %.0f for %d shares, available %.0f. Skipping.",
                        sym, required, qty, available)
            return
        if fitted < qty:
            log.warning("%s: funds only allow %d of %d shares (need %.0f, available %.0f)",
                        sym, fitted, qty, required, available)
        risk = fitted * ltp * self.s.stop_loss_pct / 100 if self.s.stop_loss_pct else None
        log.info("%s: sizing %d shares @ ~%.2f = %.0f value%s", sym, fitted, ltp, fitted * ltp,
                 f", risk to stop ~{risk:.0f}" if risk else "")
        self._execute(sym, target_dir * fitted, reason, opening=True, ltp=ltp)

    # ---------- money limits ----------
    def open_exposure(self) -> float:
        """Rupee value of all open positions (at entry prices)."""
        return sum(abs(p["qty"]) * p["avg_price"] for p in self.positions.values())

    def _fit_total_capital(self, sym: str, qty: int, price: float) -> int:
        """MAX_TOTAL_CAPITAL: shrink a new position so all open positions stay under the cap."""
        cap = self.s.max_total_capital
        if cap <= 0:
            return qty
        room = cap - self.open_exposure()
        allowed = max(int(room // price), 0)
        if allowed == 0:
            log.warning("%s: MAX_TOTAL_CAPITAL reached - %.0f already invested of %.0f, skipping",
                        sym, self.open_exposure(), cap)
        elif allowed < qty:
            log.warning("%s: MAX_TOTAL_CAPITAL allows %d of %d shares (%.0f of %.0f left)",
                        sym, allowed, qty, room, cap)
        return min(qty, allowed)

    def todays_pnl(self) -> float:
        """Realized P&L today plus the open positions' current profit/loss."""
        total = self.realized_today
        for sym, p in self.positions.items():
            if p["qty"]:
                try:
                    total += (self.ltp(sym) - p["avg_price"]) * p["qty"]
                except Exception as e:
                    log.warning("%s: no price for daily-loss check (%s)", sym, e)
        return total

    def daily_loss_reached(self) -> bool:
        """MAX_DAILY_LOSS: no new positions once today's loss hits the limit (exits still run)."""
        limit = self.s.max_daily_loss
        if limit <= 0:
            return False
        pnl = self.todays_pnl()
        if pnl > -limit:
            return False
        if not self._daily_loss_logged:
            log.warning("MAX_DAILY_LOSS reached: today's P&L %.2f <= -%.0f - no new trades today", pnl, limit)
            self._daily_loss_logged = True
        return True

    # ---------- funds ----------
    def available_funds(self) -> float:
        if self.mode == "paper":
            return self.s.paper_funds - self.open_exposure()
        return float(self.kite.margins("equity")["net"])

    def _required_margin(self, sym: str, side: str, qty: int, price: float) -> float:
        """Margin Zerodha will block for this order (live), or its full value (paper)."""
        if self.mode == "paper":
            return qty * price
        try:
            r = self.kite.order_margins([{
                "exchange": self.s.exchange, "tradingsymbol": sym,
                "transaction_type": side, "variety": "regular",
                "product": self.s.product, "order_type": "LIMIT",
                "quantity": qty, "price": price,
            }])
            return float(r[0]["total"])
        except Exception as e:
            log.warning("%s: margin lookup failed (%s), assuming full value", sym, e)
            return qty * price

    def _execute(self, sym: str, signed_qty: int, reason: str, opening: bool,
                 ltp: float | None = None) -> None:
        side = "BUY" if signed_qty > 0 else "SELL"
        qty = abs(signed_qty)
        ltp = ltp if ltp is not None else self.ltp(sym)

        if self.mode == "paper":
            filled_qty, fill_price = qty, ltp
            log.info("[PAPER] %s %d %s @ %.2f (%s)", side, qty, sym, fill_price, reason)
        else:
            filled_qty, fill_price = self._place_live(sym, side, qty, ltp, reason)
            if filled_qty == 0:
                return

        signed = filled_qty if side == "BUY" else -filled_qty
        pnl = self._apply_fill(sym, signed, fill_price)
        self.realized_today += pnl
        if opening:
            self.trades_today += 1
        self._save_state()
        self._log_trade(sym, side, filled_qty, fill_price, reason, pnl)

    def _place_live(self, sym: str, side: str, qty: int, ltp: float, reason: str) -> tuple[int, float]:
        """Places a marketable limit order and waits for it to fill."""
        self._guard(sym)
        tick = self.instruments[sym]["tick_size"]
        buf = self.s.limit_buffer_pct / 100
        price = round_to_tick(ltp * (1 + buf) if side == "BUY" else ltp * (1 - buf), tick)
        k = self.kite
        try:
            order_id = k.place_order(
                variety=k.VARIETY_REGULAR,
                exchange=self.s.exchange,
                tradingsymbol=sym,
                transaction_type=k.TRANSACTION_TYPE_BUY if side == "BUY" else k.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=k.PRODUCT_MIS if self.s.product == "MIS" else k.PRODUCT_CNC,
                order_type=k.ORDER_TYPE_LIMIT,
                price=price,
                validity=k.VALIDITY_DAY,
                tag="macross",
            )
        except Exception as e:
            log.error("[LIVE] Order FAILED %s %d %s: %s", side, qty, sym, e)
            return 0, 0.0
        log.info("[LIVE] Placed %s %d %s limit %.2f, order_id=%s (%s)",
                 side, qty, sym, price, order_id, reason)

        deadline = _time.time() + ORDER_FILL_TIMEOUT
        last = {}
        while _time.time() < deadline:
            _time.sleep(1)
            last = k.order_history(order_id)[-1]
            if last["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                break
        else:
            log.warning("[LIVE] Order %s not filled in %ds, cancelling", order_id, ORDER_FILL_TIMEOUT)
            try:
                k.cancel_order(variety=k.VARIETY_REGULAR, order_id=order_id)
            except Exception as e:
                log.error("[LIVE] Cancel failed for %s: %s", order_id, e)
            _time.sleep(1)
            last = k.order_history(order_id)[-1]

        filled = int(last.get("filled_quantity") or 0)
        avg = float(last.get("average_price") or 0)
        if last.get("status") == "REJECTED":
            log.error("[LIVE] Order %s REJECTED: %s", order_id, last.get("status_message"))
        elif filled < qty:
            log.warning("[LIVE] Order %s filled %d/%d", order_id, filled, qty)
        return filled, avg

    def _apply_fill(self, sym: str, signed_qty: int, price: float) -> float:
        """Updates position and returns realized P&L from this fill."""
        pos = self.positions.setdefault(sym, {"qty": 0, "avg_price": 0.0})
        old_qty, avg = pos["qty"], pos["avg_price"]
        new_qty = old_qty + signed_qty
        pnl = 0.0

        if old_qty == 0 or (old_qty > 0) == (signed_qty > 0):
            # opening or adding
            pos["avg_price"] = (avg * abs(old_qty) + price * abs(signed_qty)) / abs(new_qty)
        else:
            closed = min(abs(old_qty), abs(signed_qty))
            direction = 1 if old_qty > 0 else -1
            pnl = (price - avg) * closed * direction
            if new_qty == 0:
                pos["avg_price"] = 0.0
            elif (new_qty > 0) != (old_qty > 0):
                pos["avg_price"] = price  # flipped side
        pos["qty"] = new_qty
        return pnl

    def _log_trade(self, sym, side, qty, price, reason, pnl) -> None:
        new_file = not TRADES_CSV.exists()
        with TRADES_CSV.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "mode", "symbol", "side", "qty", "price", "reason", "realized_pnl"])
            w.writerow([datetime.now(IST).isoformat(timespec="seconds"), self.mode, sym,
                        side, qty, f"{price:.2f}", reason, f"{pnl:.2f}"])
        if pnl:
            log.info("%s realized P&L: %.2f", sym, pnl)

    def square_off_all(self, reason: str = "square-off") -> None:
        for sym, pos in list(self.positions.items()):
            if pos["qty"] != 0:
                self.set_target(sym, 0, reason)
