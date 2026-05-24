from __future__ import annotations

from dataclasses import dataclass

from ..portfolio.account_state import AccountState, Position
from ..utils.logger import get
from .order_intent import OrderIntent

log = get(__name__)


@dataclass
class DryRunConfig:
    slippage_bps: float
    taker_bps: float
    maker_bps: float = 0.0


@dataclass
class SimulatedFill:
    symbol: str
    side: str
    size_traded: float        # signed
    fill_price: float
    fee_usdc: float
    realized_pnl: float
    new_size: float
    new_entry_price: float


class DryRunEngine:
    """Simulates fills at reference mid ± slippage; updates AccountState in place.

    This engine NEVER makes network calls. It is the only execution path in v1.
    """

    def __init__(self, cfg: DryRunConfig):
        self.cfg = cfg

    def simulate(self, intents: list[OrderIntent], account: AccountState) -> list[SimulatedFill]:
        fills: list[SimulatedFill] = []
        for intent in intents:
            if intent.delta_size == 0.0 or intent.reference_mid <= 0:
                continue
            sign = 1.0 if intent.delta_size > 0 else -1.0
            slip = self.cfg.slippage_bps / 1e4
            fill_price = intent.reference_mid * (1.0 + sign * slip)
            notional = abs(intent.delta_size) * fill_price
            fee = notional * self.cfg.taker_bps / 1e4

            pos = account.positions.get(intent.symbol) or Position(symbol=intent.symbol)
            prior_size = pos.size
            prior_entry = pos.entry_price
            new_size = prior_size + intent.delta_size

            realized = 0.0
            # Reduction or flip realizes PnL on the closed portion
            if prior_size != 0.0 and (prior_size > 0) != (intent.delta_size > 0):
                closed = min(abs(prior_size), abs(intent.delta_size))
                direction = 1.0 if prior_size > 0 else -1.0
                realized = closed * direction * (fill_price - prior_entry)

            if new_size == 0.0:
                pos.size = 0.0
                pos.entry_price = 0.0
                pos.notional_at_entry = 0.0
            elif prior_size == 0.0 or (prior_size > 0) != (new_size > 0):
                # opened fresh or flipped → new entry is fill price
                pos.size = new_size
                pos.entry_price = fill_price
                pos.notional_at_entry = abs(new_size) * fill_price
            elif abs(new_size) > abs(prior_size):
                # weighted-average entry on add
                added = abs(intent.delta_size)
                pos.entry_price = (abs(prior_size) * prior_entry + added * fill_price) / abs(new_size)
                pos.size = new_size
                pos.notional_at_entry = abs(new_size) * pos.entry_price
            else:
                # partial reduction; keep entry
                pos.size = new_size

            account.positions[intent.symbol] = pos
            account.equity_usdc += realized - fee
            account.realized_pnl_today += realized - fee

            fills.append(SimulatedFill(
                symbol=intent.symbol,
                side=intent.side,
                size_traded=intent.delta_size,
                fill_price=fill_price,
                fee_usdc=fee,
                realized_pnl=realized,
                new_size=pos.size,
                new_entry_price=pos.entry_price,
            ))
            log.info("dry_run fill", extra={
                "symbol": intent.symbol, "side": intent.side, "action": intent.action,
                "size": intent.delta_size, "px": fill_price, "fee": fee, "realized": realized,
            })
        return fills
