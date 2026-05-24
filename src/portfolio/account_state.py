from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    size: float = 0.0           # signed; + long, - short
    entry_price: float = 0.0
    notional_at_entry: float = 0.0

    @property
    def side(self) -> str:
        if self.size > 0:
            return "LONG"
        if self.size < 0:
            return "SHORT"
        return "FLAT"


@dataclass
class AccountState:
    equity_usdc: float = 0.0
    day_start_equity_usdc: float = 0.0
    realized_pnl_today: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)

    def gross_notional(self, mids: dict[str, float]) -> float:
        return sum(abs(p.size) * mids.get(p.symbol, p.entry_price) for p in self.positions.values())

    def net_notional(self, mids: dict[str, float]) -> float:
        return sum(p.size * mids.get(p.symbol, p.entry_price) for p in self.positions.values())

    def unrealized_pnl(self, mids: dict[str, float]) -> float:
        total = 0.0
        for p in self.positions.values():
            mid = mids.get(p.symbol, p.entry_price)
            total += p.size * (mid - p.entry_price)
        return total

    def mtm_equity(self, mids: dict[str, float]) -> float:
        return self.equity_usdc + self.unrealized_pnl(mids)

    def to_dict(self) -> dict:
        return {
            "equity_usdc": self.equity_usdc,
            "day_start_equity_usdc": self.day_start_equity_usdc,
            "realized_pnl_today": self.realized_pnl_today,
            "positions": {
                s: {"size": p.size, "entry_price": p.entry_price,
                    "notional_at_entry": p.notional_at_entry}
                for s, p in self.positions.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        st = cls(
            equity_usdc=float(d.get("equity_usdc", 0.0)),
            day_start_equity_usdc=float(d.get("day_start_equity_usdc", 0.0)),
            realized_pnl_today=float(d.get("realized_pnl_today", 0.0)),
        )
        for sym, pd in (d.get("positions") or {}).items():
            st.positions[sym] = Position(
                symbol=sym,
                size=float(pd.get("size", 0.0)),
                entry_price=float(pd.get("entry_price", 0.0)),
                notional_at_entry=float(pd.get("notional_at_entry", 0.0)),
            )
        return st
