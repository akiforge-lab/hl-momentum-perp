from __future__ import annotations

from ..portfolio.account_state import AccountState
from ..portfolio.basket import TargetPosition
from .order_intent import OrderIntent


def diff_to_intents(
    targets: list[TargetPosition],
    account: AccountState,
    mids: dict[str, float],
    rebalance_drift_pct: float,
) -> list[OrderIntent]:
    """Compare desired basket vs current positions and emit OrderIntents."""
    target_by_sym = {t.symbol: t for t in targets}
    intents: list[OrderIntent] = []

    # Open / adjust positions for targeted symbols
    for sym, t in target_by_sym.items():
        mid = mids.get(sym, 0.0)
        if mid <= 0:
            continue
        sign = 1.0 if t.side == "LONG" else -1.0
        target_size = sign * (t.notional_usdc / mid)
        current_size = account.positions[sym].size if sym in account.positions else 0.0
        current_notional = abs(current_size) * mid

        if current_size == 0.0:
            action = "OPEN"
        elif (current_size > 0) != (target_size > 0):
            action = "FLIP"
        elif abs(target_size) > abs(current_size):
            action = "INCREASE"
        else:
            action = "REDUCE"

        # drift gate
        if current_size != 0.0:
            drift_pct = abs(t.notional_usdc - current_notional) / max(current_notional, 1e-9) * 100.0
            same_side = (current_size > 0) == (target_size > 0)
            if same_side and drift_pct < rebalance_drift_pct:
                continue

        intents.append(OrderIntent(
            symbol=sym,
            side=t.side,
            action=action,
            target_notional_usdc=t.notional_usdc,
            target_size=target_size,
            current_size=current_size,
            delta_size=target_size - current_size,
            reference_mid=mid,
            score=t.score,
            weight=t.weight,
            reason=f"basket:{t.side.lower()}_sleeve",
        ))

    # Close positions that fell out of the basket
    for sym, pos in account.positions.items():
        if sym in target_by_sym or pos.size == 0.0:
            continue
        mid = mids.get(sym, pos.entry_price)
        side = "LONG" if pos.size > 0 else "SHORT"
        intents.append(OrderIntent(
            symbol=sym,
            side=side,
            action="CLOSE",
            target_notional_usdc=0.0,
            target_size=0.0,
            current_size=pos.size,
            delta_size=-pos.size,
            reference_mid=mid,
            reason="out_of_basket",
        ))

    return intents
