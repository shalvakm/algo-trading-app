"""How many shares to buy/sell when opening a position.

Shared by the live broker and the backtester so both size trades the same way.
"""
import math

from config import Settings


def position_size(price: float, s: Settings, stop_pct: float | None = None) -> int:
    """Shares for a new position, before checking available funds.

    Risk-based (RISK_PER_TRADE > 0): hitting the stop-loss loses ~RISK_PER_TRADE rupees.
        shares = RISK_PER_TRADE / (price * stop% / 100), stop% = this trade's stop distance (or STOP_LOSS_PCT)
    Otherwise: the fixed QUANTITY.
    Either way, capped so the position is worth at most MAX_CAPITAL_PER_TRADE.
    """
    if price <= 0:
        return 0
    pct = stop_pct if stop_pct else s.stop_loss_pct
    if s.risk_per_trade > 0 and pct > 0:
        qty = math.floor(s.risk_per_trade / (price * pct / 100))
    else:
        qty = s.quantity
    if s.max_capital_per_trade > 0:
        qty = min(qty, math.floor(s.max_capital_per_trade / price))
    return max(qty, 0)


def fit_to_funds(qty: int, required: float, available: float, headroom: float = 0.98) -> int:
    """Scale qty down so the margin `required` for it fits within `available` funds."""
    if qty <= 0 or required <= available * headroom:
        return max(qty, 0)
    per_share = required / qty
    return max(math.floor(available * headroom / per_share), 0)
