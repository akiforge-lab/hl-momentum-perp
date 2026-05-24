from src.execution.order_intent import OrderIntent
from src.portfolio.account_state import AccountState
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskConfig, RiskManager


def _risk(**over):
    cfg = RiskConfig(
        max_leverage=2.0, max_gross_x_equity=2.0, max_net_abs_x_equity=0.3,
        max_per_symbol_pct_of_equity=15.0, min_liq_distance_pct=10.0,
        stale_data_sec=900, max_daily_loss_pct=3.0,
        stop_loss_cooldown_min=60, daily_loss_cooldown_min=1440,
        funding_skip_abs_bps_per_hr=8.0,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return RiskManager(cfg, KillSwitch("/tmp/__nokill__"))


def _intent(sym, side, notional, mid):
    sign = 1.0 if side == "LONG" else -1.0
    return OrderIntent(symbol=sym, side=side, action="OPEN",
                       target_notional_usdc=notional,
                       target_size=sign * notional / mid,
                       current_size=0.0,
                       delta_size=sign * notional / mid,
                       reference_mid=mid)


def test_per_symbol_cap_blocks_oversize():
    r = _risk()
    acct = AccountState(equity_usdc=1000.0, day_start_equity_usdc=1000.0)
    intents = [_intent("BTC", "LONG", 500, 50000)]  # 50% of equity, cap 15%
    d = r.gate(intents, account=acct, mids={"BTC": 50000}, max_leverages={"BTC": 50},
               funding_bps_per_hr={}, market_data_age_sec=1.0)
    assert d.accepted == []
    assert d.rejected and "per_symbol_cap" in d.rejected[0][1]


def test_kill_switch_blocks_all():
    r = _risk()
    r.kill.trip("test")
    acct = AccountState(equity_usdc=1000.0)
    intents = [_intent("BTC", "LONG", 50, 50000)]
    d = r.gate(intents, account=acct, mids={"BTC": 50000}, max_leverages={"BTC": 50},
               funding_bps_per_hr={}, market_data_age_sec=1.0)
    assert d.accepted == [] and d.rejected[0][1].startswith("kill:")


def test_adverse_funding_blocks_long():
    r = _risk(funding_skip_abs_bps_per_hr=5.0)
    acct = AccountState(equity_usdc=1000.0)
    intents = [_intent("BTC", "LONG", 50, 50000)]
    d = r.gate(intents, account=acct, mids={"BTC": 50000}, max_leverages={"BTC": 50},
               funding_bps_per_hr={"BTC": 10.0}, market_data_age_sec=1.0)
    assert "adverse_funding_long" in d.rejected[0][1]
