"""How many shares to buy/sell when opening a position.

Shared by the live broker and the backtester so both size trades the same way.
"""
import math

from config import Settings


def position_size(price: float, s: Settings) -> int:
    """Shares for a new position, before checking available funds.

    Risk-based (RISK_PER_TRADE > 0): hitting the stop-loss loses ~RISK_PER_TRADE rupees.
        shares = RISK_PER_TRADE / (price * STOP_LOSS_PCT / 100)
    Otherwise: the fixed QUANTITY.
    Either way, capped so the position is worth at most MAX_CAPITAL_PER_TRADE.
    """
    if price <= 0:
        return 0
    if s.risk_per_trade > 0:
        qty = math.floor(s.risk_per_trade / (price * s.stop_loss_pct / 100))
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
