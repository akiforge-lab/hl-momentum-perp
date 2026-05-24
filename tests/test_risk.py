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


def _close_intent(sym, side, entry_size, ref_mid=0.0):
    return OrderIntent(
        symbol=sym, side=side, action="CLOSE",
        target_notional_usdc=0.0, target_size=0.0,
        current_size=entry_size, delta_size=-entry_size,
        reference_mid=ref_mid,
    )


def test_close_without_current_mid_uses_reference_mid():
    from src.portfolio.account_state import Position
    r = _risk()
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)
    acct.positions["ZRO"] = Position(symbol="ZRO", size=-100.0, entry_price=1.3, notional_at_entry=130.0)
    intent = _close_intent("ZRO", "SHORT", -100.0, ref_mid=1.25)
    d = r.gate([intent], account=acct, mids={},  # NO mid for ZRO
               max_leverages={"ZRO": 10}, funding_bps_per_hr={},
               market_data_age_sec=1.0)
    assert d.rejected == []
    assert d.accepted and d.accepted[0].symbol == "ZRO"
    assert d.accepted[0].reference_mid == 1.25, "should use reference_mid fallback"


def test_close_falls_back_to_entry_price_when_ref_mid_also_missing():
    from src.portfolio.account_state import Position
    r = _risk()
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)
    acct.positions["ZRO"] = Position(symbol="ZRO", size=-100.0, entry_price=1.3, notional_at_entry=130.0)
    intent = _close_intent("ZRO", "SHORT", -100.0, ref_mid=0.0)
    d = r.gate([intent], account=acct, mids={},
               max_leverages={"ZRO": 10}, funding_bps_per_hr={},
               market_data_age_sec=1.0)
    assert d.accepted and d.accepted[0].reference_mid == 1.3


def test_close_rejected_when_no_price_anywhere():
    r = _risk()
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)
    # No position exists either → no entry_price fallback.
    intent = _close_intent("PHANTOM", "SHORT", -100.0, ref_mid=0.0)
    d = r.gate([intent], account=acct, mids={},
               max_leverages={"PHANTOM": 10}, funding_bps_per_hr={},
               market_data_age_sec=1.0)
    assert d.accepted == []
    assert d.rejected and d.rejected[0][1] == "no_close_price"


def test_open_with_no_mid_still_rejected():
    r = _risk()
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)
    intent = _intent("FOO", "LONG", 500, 50)  # builder fills reference_mid=50
    d = r.gate([intent], account=acct, mids={},  # but mids missing
               max_leverages={"FOO": 10}, funding_bps_per_hr={},
               market_data_age_sec=1.0)
    assert d.accepted == []
    assert d.rejected and d.rejected[0][1] == "no_mid"


def test_close_ignores_adverse_funding_and_caps():
    """Even if funding is adverse and the position is huge, CLOSE must pass."""
    from src.portfolio.account_state import Position
    r = _risk(funding_skip_abs_bps_per_hr=1.0, max_per_symbol_pct_of_equity=1.0)
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)
    acct.positions["BTC"] = Position(symbol="BTC", size=1.0, entry_price=50000.0, notional_at_entry=50000.0)
    intent = _close_intent("BTC", "LONG", 1.0, ref_mid=50000.0)
    d = r.gate([intent], account=acct, mids={"BTC": 50000.0},
               max_leverages={"BTC": 50},
               funding_bps_per_hr={"BTC": 9999.0},  # absurdly adverse
               market_data_age_sec=1.0)
    assert d.accepted and not d.rejected


def test_adverse_funding_blocks_long():
    r = _risk(funding_skip_abs_bps_per_hr=5.0)
    acct = AccountState(equity_usdc=1000.0)
    intents = [_intent("BTC", "LONG", 50, 50000)]
    d = r.gate(intents, account=acct, mids={"BTC": 50000}, max_leverages={"BTC": 50},
               funding_bps_per_hr={"BTC": 10.0}, market_data_age_sec=1.0)
    assert "adverse_funding_long" in d.rejected[0][1]
