"""Estimated Zerodha charges per executed order (equity, NSE).

Approximate rates - check Zerodha's brokerage calculator (zerodha.com/brokerage-calculator) and update
here if they change. Slippage (fills worse than the expected price) is not included.

    MIS (intraday):  brokerage 0.03% or Rs 20 per order (lower) | STT 0.025% sell side |
                     stamp duty 0.003% buy side
    CNC (delivery):  brokerage 0 | STT 0.1% buy and sell | stamp duty 0.015% buy side |
                     DP charge ~Rs 15.93 per stock on the sell day
    Both:            exchange charges ~0.00297% | SEBI fee Rs 10 per crore |
                     GST 18% on brokerage + exchange + SEBI fees
"""
EXCHANGE = 0.0000297
SEBI_PER_RUPEE = 10 / 1e7
GST = 0.18
DP_CHARGE = 15.93


def order_charges(value: float, side: str, product: str = "MIS") -> float:
    """Estimated total charges (rupees) for one executed order of `value` rupees."""
    sell = side.upper() == "SELL"
    exch, sebi = EXCHANGE * value, SEBI_PER_RUPEE * value
    if product.upper() == "CNC":
        brokerage = 0.0
        stt = 0.001 * value
        stamp = 0.0 if sell else 0.00015 * value
        dp = DP_CHARGE if sell else 0.0
    else:
        brokerage = min(0.0003 * value, 20.0)
        stt = 0.00025 * value if sell else 0.0
        stamp = 0.0 if sell else 0.00003 * value
        dp = 0.0
    return round(brokerage + stt + exch + sebi + stamp + GST * (brokerage + exch + sebi) + dp, 2)
