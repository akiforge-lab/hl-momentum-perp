from src.execution.dry_run_engine import DryRunConfig, DryRunEngine
from src.execution.order_intent import OrderIntent
from src.portfolio.account_state import AccountState


def test_open_long_then_close_realizes_pnl():
    eng = DryRunEngine(DryRunConfig(slippage_bps=0.0, taker_bps=0.0))
    acct = AccountState(equity_usdc=10000.0, day_start_equity_usdc=10000.0)

    open_intent = OrderIntent(
        symbol="BTC", side="LONG", action="OPEN",
        target_notional_usdc=1000.0, target_size=0.02, current_size=0.0,
        delta_size=0.02, reference_mid=50000.0,
    )
    eng.simulate([open_intent], acct)
    assert acct.positions["BTC"].size == 0.02
    assert acct.positions["BTC"].entry_price == 50000.0

    close_intent = OrderIntent(
        symbol="BTC", side="SHORT", action="CLOSE",
        target_notional_usdc=0.0, target_size=0.0, current_size=0.02,
        delta_size=-0.02, reference_mid=55000.0,
    )
    fills = eng.simulate([close_intent], acct)
    assert acct.positions["BTC"].size == 0.0
    assert fills[0].realized_pnl == 0.02 * (55000.0 - 50000.0)
    assert acct.realized_pnl_today == 0.02 * (55000.0 - 50000.0)
