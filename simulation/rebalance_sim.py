"""Deterministic rebalance simulation. No network, no signing.

Drives the real basket / diff / risk / dry-run modules with synthetic momentum
scores and mids. Prints per-scenario state so the rebalance/rotation/flip
behavior can be inspected in one place.

Run: python -m simulation.rebalance_sim
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.execution.dry_run_engine import DryRunConfig, DryRunEngine
from src.execution.intent_builder import diff_to_intents
from src.portfolio.account_state import AccountState
from src.portfolio.basket import build_basket, scale_basket_to_caps
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskConfig, RiskManager
from src.signal.momentum import MomentumScore


# -- fixed test universe; 10 symbols, deterministic scores & mids -----------

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE",
            "FFF", "GGG", "HHH", "III", "JJJ"]

MIDS_T0 = {s: 100.0 + 10 * i for i, s in enumerate(UNIVERSE)}  # 100,110,...,190
MAX_LEV = {s: 50 for s in UNIVERSE}
FUNDING = {s: 0.0 for s in UNIVERSE}

EQUITY = 10_000.0
TARGET_LEV = 1.0
LONGS_K = 5
SHORTS_K = 5
DRIFT_PCT = 25.0


def make_risk() -> RiskManager:
    cfg = RiskConfig(
        max_leverage=2.0, max_gross_x_equity=2.0, max_net_abs_x_equity=0.30,
        max_per_symbol_pct_of_equity=25.0, min_liq_distance_pct=10.0,
        stale_data_sec=900, max_daily_loss_pct=10.0,
        stop_loss_cooldown_min=60, daily_loss_cooldown_min=1440,
        funding_skip_abs_bps_per_hr=20.0,
    )
    kill = KillSwitch("/tmp/__rebalance_sim_no_kill__")
    return RiskManager(cfg, kill)


def make_engine() -> DryRunEngine:
    return DryRunEngine(DryRunConfig(slippage_bps=0.0, taker_bps=5.0))


def scores_v1() -> list[MomentumScore]:
    """5 strong longs (AAA..EEE) and 5 strong shorts (FFF..JJJ)."""
    return (
        [MomentumScore(s, 200 - i * 5, 0.9, (200 - i * 5) * 0.9, 90)
         for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])] +
        [MomentumScore(s, -(200 - i * 5), 0.9, -(200 - i * 5) * 0.9, 90)
         for i, s in enumerate(["FFF", "GGG", "HHH", "III", "JJJ"])]
    )


def run_cycle(label: str, scores: list[MomentumScore], mids: dict[str, float],
              account: AccountState, risk: RiskManager, engine: DryRunEngine,
              equity_override: float | None = None) -> dict:
    print(f"\n========== {label} ==========")
    from src.signal.momentum import rank
    longs, shorts = rank(scores, score_floor_abs=10.0)
    equity_for_sizing = equity_override if equity_override is not None else account.equity_usdc
    raw = build_basket(longs, shorts, longs_k=LONGS_K, shorts_k=SHORTS_K,
                       weighting="equal", equity_usdc=equity_for_sizing,
                       target_leverage=TARGET_LEV, mode="independent")
    targets, scale_info = scale_basket_to_caps(
        raw, equity_usdc=equity_for_sizing,
        max_gross_x_equity=2.0, max_net_abs_x_equity=0.30, max_leverage=2.0,
    )
    if scale_info.get("scaled"):
        print(f"  basket scaled: {scale_info}")

    intents = diff_to_intents(targets, account, mids, DRIFT_PCT)
    decision = risk.gate(intents, account=account, mids=mids,
                         max_leverages=MAX_LEV, funding_bps_per_hr=FUNDING,
                         market_data_age_sec=1.0)
    print(f"  intents: {len(intents)}   accepted: {len(decision.accepted)}   "
          f"rejected: {len(decision.rejected)}")
    for it in intents:
        print(f"    {it.action:<8} {it.side:<5} {it.symbol:<5} "
              f"delta_size={it.delta_size:+.4f}  "
              f"target_notional={it.target_notional_usdc:.2f}")
    for it, reason in decision.rejected:
        print(f"    REJECT {it.symbol}: {reason}")

    fills = engine.simulate(decision.accepted, account)
    total_fee = sum(f.fee_usdc for f in fills)
    total_realized = sum(f.realized_pnl for f in fills)
    print(f"  fills: {len(fills)}   total_fee={total_fee:.4f}   "
          f"total_realized_pnl={total_realized:+.4f}")
    print(f"  equity={account.equity_usdc:.4f}   "
          f"realized_today={account.realized_pnl_today:+.4f}   "
          f"gross={account.gross_notional(mids):.2f}   "
          f"net={account.net_notional(mids):+.2f}   "
          f"positions={len([p for p in account.positions.values() if p.size != 0])}")

    telegram_would_send = bool(decision.accepted or decision.rejected)
    print(f"  telegram_would_send: {telegram_would_send}")
    return {"intents": len(intents), "fills": len(fills),
            "telegram": telegram_would_send,
            "fee": total_fee, "realized": total_realized}


def main() -> None:
    account = AccountState(equity_usdc=EQUITY, day_start_equity_usdc=EQUITY)
    risk = make_risk()
    engine = make_engine()

    # ---- Scenario A: cold start -> open full 5L/5S basket
    r = run_cycle("A: cold start (empty -> 5L/5S)", scores_v1(), MIDS_T0,
                  account, risk, engine)
    assert r["fills"] == 10, "expected 10 opens"
    assert len([p for p in account.positions.values() if p.size > 0]) == 5
    assert len([p for p in account.positions.values() if p.size < 0]) == 5

    # ---- Scenario B: identical signals + mids -> no churn
    r = run_cycle("B: identical re-run (no-change basket)", scores_v1(), MIDS_T0,
                  account, risk, engine)
    assert r["intents"] == 0 and r["fills"] == 0, "no-change cycle must be a no-op"
    assert r["telegram"] is False, "telegram must NOT send when nothing changed"

    # ---- Scenario C: small drift below threshold -> still no-op
    #     simulate by bumping mids slightly so notional drifts ~5%
    mids_small = {s: m * 1.05 for s, m in MIDS_T0.items()}
    r = run_cycle("C1: ~5% mid drift (below 25% threshold)", scores_v1(), mids_small,
                  account, risk, engine)
    assert r["fills"] == 0, "drift < threshold must not trigger fills"

    # ---- Scenario C2: large drift -> resize (no rotation, no flip)
    #     bump equity 50% so target notionals jump ~50% -> above threshold
    r = run_cycle("C2: equity +50% (target notionals up 50% -> resize all)",
                  scores_v1(), MIDS_T0, account, risk, engine,
                  equity_override=EQUITY * 1.5)
    # all 10 should resize (INCREASE), none CLOSE/FLIP, no new symbols
    assert r["fills"] == 10
    assert len([p for p in account.positions.values() if p.size != 0]) == 10

    # ---- Scenario D: rotation - drop EEE (long) and JJJ (short), add new names
    rot_scores = (
        [MomentumScore("AAA", 200, 0.9, 180, 90),
         MomentumScore("BBB", 195, 0.9, 175.5, 90),
         MomentumScore("CCC", 190, 0.9, 171, 90),
         MomentumScore("DDD", 185, 0.9, 166.5, 90),
         MomentumScore("KKK", 220, 0.9, 198, 90)] +  # NEW long, drops EEE
        [MomentumScore("FFF", -200, 0.9, -180, 90),
         MomentumScore("GGG", -195, 0.9, -175.5, 90),
         MomentumScore("HHH", -190, 0.9, -171, 90),
         MomentumScore("III", -185, 0.9, -166.5, 90),
         MomentumScore("LLL", -220, 0.9, -198, 90)]   # NEW short, drops JJJ
    )
    rot_mids = dict(MIDS_T0)
    rot_mids["KKK"] = 120.0
    rot_mids["LLL"] = 80.0
    MAX_LEV["KKK"] = 50; MAX_LEV["LLL"] = 50
    FUNDING["KKK"] = 0.0; FUNDING["LLL"] = 0.0
    r = run_cycle("D: rotation (drop EEE & JJJ, add KKK & LLL)",
                  rot_scores, rot_mids, account, risk, engine,
                  equity_override=EQUITY * 1.5)
    syms = {s for s, p in account.positions.items() if p.size != 0}
    assert "EEE" not in syms and "JJJ" not in syms
    assert "KKK" in syms and "LLL" in syms
    assert len(syms) == 10
    # Closed symbols must be PRUNED from the dict (not left as size=0 residuals).
    assert "EEE" not in account.positions, "closed long EEE must be pruned"
    assert "JJJ" not in account.positions, "closed short JJJ must be pruned"
    assert len(account.positions) == 10, \
        f"positions dict must equal active count (got {len(account.positions)})"

    # ---- Scenario E: side flip - AAA flips LONG -> SHORT, FFF flips SHORT -> LONG
    flip_scores = (
        [MomentumScore("BBB", 195, 0.9, 175.5, 90),
         MomentumScore("CCC", 190, 0.9, 171, 90),
         MomentumScore("DDD", 185, 0.9, 166.5, 90),
         MomentumScore("KKK", 220, 0.9, 198, 90),
         MomentumScore("FFF", 230, 0.9, 207, 90)] +  # FFF flipped LONG
        [MomentumScore("GGG", -195, 0.9, -175.5, 90),
         MomentumScore("HHH", -190, 0.9, -171, 90),
         MomentumScore("III", -185, 0.9, -166.5, 90),
         MomentumScore("LLL", -220, 0.9, -198, 90),
         MomentumScore("AAA", -230, 0.9, -207, 90)]   # AAA flipped SHORT
    )
    r = run_cycle("E: side flip (AAA L->S, FFF S->L)",
                  flip_scores, rot_mids, account, risk, engine,
                  equity_override=EQUITY * 1.5)
    aaa = account.positions["AAA"]; fff = account.positions["FFF"]
    assert aaa.size < 0, f"AAA should be SHORT after flip, got size={aaa.size}"
    assert fff.size > 0, f"FFF should be LONG after flip, got size={fff.size}"
    assert len(account.positions) == 10, \
        f"flip must not leave residuals (got {len(account.positions)} entries)"

    # ---- Coherence summary
    print("\n========== COHERENCE ==========")
    print(f"final equity:           {account.equity_usdc:.4f}")
    print(f"realized PnL today:     {account.realized_pnl_today:+.4f}")
    print(f"open positions:         {len([p for p in account.positions.values() if p.size != 0])}")
    print(f"gross notional:         {account.gross_notional(rot_mids):.2f}")
    print(f"net notional:           {account.net_notional(rot_mids):+.2f}")
    print(f"equity drift from start: {account.equity_usdc - EQUITY:+.4f}  "
          f"(should equal realized_today)")
    assert abs((account.equity_usdc - EQUITY) - account.realized_pnl_today) < 1e-6, \
        "equity delta must equal realized PnL (no other source of change in dry-run)"
    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
