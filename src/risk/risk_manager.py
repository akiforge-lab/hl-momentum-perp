from __future__ import annotations

from dataclasses import dataclass

from ..execution.order_intent import OrderIntent
from ..portfolio.account_state import AccountState
from ..utils.logger import get
from ..utils.time import utcnow_ts
from .kill_switch import KillSwitch
from .liquidation import liq_distance_pct, liquidation_price

log = get(__name__)


@dataclass
class RiskConfig:
    max_leverage: float
    max_gross_x_equity: float
    max_net_abs_x_equity: float
    max_per_symbol_pct_of_equity: float
    min_liq_distance_pct: float
    stale_data_sec: int
    max_daily_loss_pct: float
    stop_loss_cooldown_min: int
    daily_loss_cooldown_min: int
    funding_skip_abs_bps_per_hr: float


@dataclass
class RiskDecision:
    accepted: list[OrderIntent]
    rejected: list[tuple[OrderIntent, str]]


class RiskManager:
    def __init__(self, cfg: RiskConfig, kill: KillSwitch):
        self.cfg = cfg
        self.kill = kill
        self.cooldown_until_ts: float = 0.0
        self.symbol_cooldowns: dict[str, float] = {}

    # ---- cooldowns -------------------------------------------------------

    def start_global_cooldown(self, minutes: int, reason: str) -> None:
        self.cooldown_until_ts = max(self.cooldown_until_ts, utcnow_ts() + minutes * 60)
        log.warning("global cooldown started", extra={"minutes": minutes, "reason": reason})

    def start_symbol_cooldown(self, symbol: str, minutes: int, reason: str) -> None:
        self.symbol_cooldowns[symbol] = max(
            self.symbol_cooldowns.get(symbol, 0.0), utcnow_ts() + minutes * 60
        )
        log.warning("symbol cooldown started", extra={"symbol": symbol, "minutes": minutes, "reason": reason})

    def _global_cooldown_active(self) -> bool:
        return utcnow_ts() < self.cooldown_until_ts

    def _symbol_cooldown_active(self, symbol: str) -> bool:
        return utcnow_ts() < self.symbol_cooldowns.get(symbol, 0.0)

    # ---- daily loss ------------------------------------------------------

    def check_daily_loss(self, account: AccountState, mids: dict[str, float]) -> bool:
        if account.day_start_equity_usdc <= 0:
            return False
        mtm = account.mtm_equity(mids)
        pnl_pct = (mtm - account.day_start_equity_usdc) / account.day_start_equity_usdc * 100.0
        if pnl_pct <= -self.cfg.max_daily_loss_pct:
            self.kill.trip(f"daily_loss {pnl_pct:.2f}%")
            self.start_global_cooldown(self.cfg.daily_loss_cooldown_min, "daily_loss")
            return True
        return False

    # ---- main gate -------------------------------------------------------

    def gate(
        self,
        intents: list[OrderIntent],
        *,
        account: AccountState,
        mids: dict[str, float],
        max_leverages: dict[str, int],
        funding_bps_per_hr: dict[str, float],
        market_data_age_sec: float,
    ) -> RiskDecision:
        accepted: list[OrderIntent] = []
        rejected: list[tuple[OrderIntent, str]] = []

        active, reason = self.kill.is_active()
        if active:
            return RiskDecision([], [(i, f"kill:{reason}") for i in intents])

        if market_data_age_sec > self.cfg.stale_data_sec:
            return RiskDecision([], [(i, f"stale_data:{market_data_age_sec:.0f}s") for i in intents])

        if self._global_cooldown_active():
            return RiskDecision([], [(i, "global_cooldown") for i in intents])

        equity = max(account.equity_usdc, 1e-9)

        # Aggregate caps (gross/net/leverage) are enforced upstream by the
        # portfolio scaler so the long/short sleeves stay balanced. This gate
        # only handles per-symbol reasons: cooldown, no_mid, funding,
        # per-symbol cap, liquidation distance.
        sim_positions = {s: p.size for s, p in account.positions.items()}

        for intent in intents:
            if self._symbol_cooldown_active(intent.symbol):
                rejected.append((intent, "symbol_cooldown"))
                continue

            mid = mids.get(intent.symbol, 0.0)
            if mid <= 0:
                rejected.append((intent, "no_mid"))
                continue

            fund_bps_hr = funding_bps_per_hr.get(intent.symbol, 0.0)
            if intent.side == "LONG" and fund_bps_hr > self.cfg.funding_skip_abs_bps_per_hr:
                rejected.append((intent, f"adverse_funding_long:{fund_bps_hr:.1f}bps/hr"))
                continue
            if intent.side == "SHORT" and fund_bps_hr < -self.cfg.funding_skip_abs_bps_per_hr:
                rejected.append((intent, f"adverse_funding_short:{fund_bps_hr:.1f}bps/hr"))
                continue

            target_notional = intent.target_notional_usdc
            cap_per_sym = self.cfg.max_per_symbol_pct_of_equity / 100.0 * equity
            if target_notional > cap_per_sym:
                rejected.append((intent, f"per_symbol_cap:{target_notional:.0f}>{cap_per_sym:.0f}"))
                continue

            new_size = (target_notional / mid) * (1 if intent.side == "LONG" else -1)
            sim_positions[intent.symbol] = new_size

            gross = sum(abs(s) * mids.get(sym, 0.0) for sym, s in sim_positions.items())
            eff_lev = gross / equity

            mxl = max_leverages.get(intent.symbol, 10)
            liq = liquidation_price(side=intent.side, entry_price=mid,
                                    leverage=max(eff_lev, 1.0), max_leverage=mxl)
            dist = liq_distance_pct(intent.side, mid, liq)
            if dist < self.cfg.min_liq_distance_pct:
                sim_positions[intent.symbol] = 0.0  # don't count this in further sims
                rejected.append((intent, f"liq_distance:{dist:.1f}%<{self.cfg.min_liq_distance_pct:.1f}%"))
                continue

            intent.computed_liq_price = liq
            intent.computed_liq_distance_pct = dist
            accepted.append(intent)

        # Defence-in-depth: verify aggregate caps after gating; warn loudly
        # if the scaler upstream let something through.
        gross = sum(abs(s) * mids.get(sym, 0.0) for sym, s in sim_positions.items())
        net = sum(s * mids.get(sym, 0.0) for sym, s in sim_positions.items())
        if gross > self.cfg.max_gross_x_equity * equity * 1.001:
            log.error("aggregate gross cap exceeded after gating",
                      extra={"gross": gross, "cap": self.cfg.max_gross_x_equity * equity})
        if abs(net) > self.cfg.max_net_abs_x_equity * equity * 1.001:
            log.error("aggregate net cap exceeded after gating",
                      extra={"net": net, "cap": self.cfg.max_net_abs_x_equity * equity})
        if gross / equity > self.cfg.max_leverage * 1.001:
            log.error("leverage cap exceeded after gating",
                      extra={"lev": gross / equity, "cap": self.cfg.max_leverage})

        return RiskDecision(accepted, rejected)
