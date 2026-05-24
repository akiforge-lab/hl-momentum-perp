"""Cross-margin liquidation distance estimator.

This is an APPROXIMATION suitable for pre-trade risk gating, not for production
liquidation calculations. Hyperliquid uses tiered maintenance margin; here we
use a single maintenance-margin ratio derived from `1 / maxLeverage * 0.5`,
which approximates HL's "half initial margin" maintenance rule.
"""
from __future__ import annotations


def maintenance_margin_ratio(max_leverage: int) -> float:
    if max_leverage <= 0:
        return 1.0
    return 0.5 / float(max_leverage)


def liquidation_price(
    *,
    side: str,
    entry_price: float,
    leverage: float,
    max_leverage: int,
) -> float:
    """Approximate cross-margin liquidation price for an isolated view of the
    position. Uses: liq = entry * (1 - sign / lev * (1 - mmr))."""
    if entry_price <= 0 or leverage <= 0:
        return 0.0
    mmr = maintenance_margin_ratio(max_leverage)
    sign = 1.0 if side == "LONG" else -1.0
    return entry_price * (1.0 - sign / leverage * (1.0 - mmr))


def liq_distance_pct(side: str, mark: float, liq: float) -> float:
    if mark <= 0 or liq <= 0:
        return 100.0
    if side == "LONG":
        return max(0.0, (mark - liq) / mark * 100.0)
    return max(0.0, (liq - mark) / mark * 100.0)
