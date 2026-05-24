from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal


Side = Literal["LONG", "SHORT"]
Action = Literal["OPEN", "INCREASE", "REDUCE", "CLOSE", "FLIP"]


@dataclass
class OrderIntent:
    symbol: str
    side: Side
    action: Action
    target_notional_usdc: float    # absolute, unsigned
    target_size: float             # signed; +long, -short
    current_size: float            # signed
    delta_size: float              # signed; size to trade
    reference_mid: float
    score: float = 0.0
    weight: float = 0.0
    reason: str = ""
    # populated by risk gate:
    computed_liq_price: float = 0.0
    computed_liq_distance_pct: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
