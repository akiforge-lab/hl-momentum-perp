import asyncio
from dataclasses import dataclass

import pytest

from src.execution.order_intent import OrderIntent
from src.notify.operator import Operator, OperatorConfig
from src.notify.telegram import TelegramNotifier


class FakeTelegram(TelegramNotifier):
    """In-process Telegram stand-in. Captures sends, no HTTP."""
    def __init__(self, severities):
        super().__init__(bot_token="x", chat_id="y",
                         severities=severities, rate_limit_sec=0.0)
        self.sent: list[tuple[str, str]] = []
        self.enabled = True  # pretend creds present so severity filter runs

    async def send(self, severity, text):
        if severity not in self.severities:
            return
        self.sent.append((severity, text))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_disabled_notifier_silent():
    tg = TelegramNotifier(bot_token=None, chat_id=None,
                          severities=["startup", "rotation", "warning"],
                          rate_limit_sec=0.0)
    op = Operator(tg)
    # No exception, no HTTP, no crash.
    _run(op.startup(mode="dry_run", target_leverage=1.0, max_leverage=2.0,
                    longs_k=5, shorts_k=5))


def test_rotation_summary_format():
    tg = FakeTelegram(["rotation"])
    op = Operator(tg)

    @dataclass
    class F: symbol: str; side: str; action: str

    fills = [F("ETHFI", "SHORT", "OPEN"), F("AAVE", "SHORT", "CLOSE"),
             F("BTC", "LONG", "INCREASE")]  # INCREASE must be ignored
    _run(op.rotation(fills, equity=10000.0, day_pnl=12.34))
    assert len(tg.sent) == 1
    _, msg = tg.sent[0]
    assert "Rotation" in msg
    assert "ETHFI" in msg and "AAVE" in msg
    assert "BTC" not in msg, "INCREASE/REDUCE should not appear in rotation summary"
    assert "+12.34" in msg


def test_rotation_silent_when_no_real_rotation():
    tg = FakeTelegram(["rotation"])
    op = Operator(tg)

    @dataclass
    class F: symbol: str; side: str; action: str
    _run(op.rotation([F("X", "LONG", "INCREASE")], equity=100.0, day_pnl=0.0))
    assert tg.sent == []


def _intent(sym, side):
    return OrderIntent(symbol=sym, side=side, action="OPEN",
                       target_notional_usdc=100, target_size=1, current_size=0,
                       delta_size=1, reference_mid=100)


def test_rejects_aggregated_by_reason_and_deduped():
    tg = FakeTelegram(["risk_reject", "warning"])
    op = Operator(tg, OperatorConfig(dedup_window_sec=999))

    rj = [(_intent("AAA", "LONG"), "leverage_cap:1.5x>1.0x"),
          (_intent("BBB", "SHORT"), "leverage_cap:1.5x>1.0x"),
          (_intent("CCC", "LONG"), "no_mid")]
    _run(op.rejects(rj))
    # one msg per reason class
    classes = {m.split("(")[1].split(")")[0] for _, m in tg.sent if "Risk reject" in m}
    assert classes == {"leverage_cap", "no_mid"}

    # Re-send same → dedup blocks it
    tg.sent.clear()
    _run(op.rejects(rj))
    assert tg.sent == [], "duplicate reject classes should be suppressed within dedup window"


def test_repeated_reject_loop_detected():
    tg = FakeTelegram(["risk_reject", "warning"])
    op = Operator(tg, OperatorConfig(dedup_window_sec=999,
                                     repeated_reject_cycles=3,
                                     repeated_reject_window=5))
    persistent = [(_intent("ZRO", "SHORT"), "no_mid:0")]
    for _ in range(3):
        _run(op.rejects(persistent))
    warnings = [m for s, m in tg.sent if s == "warning"]
    assert any("Repeated reject" in m and "ZRO" in m for m in warnings)


def test_gross_drift_warn():
    tg = FakeTelegram(["warning"])
    op = Operator(tg, OperatorConfig(gross_drift_warn_pct=20.0))
    _run(op.warn_gross_drift(gross=12000.0, equity=10000.0, target_lev=1.0))  # 20% drift, borderline
    _run(op.warn_gross_drift(gross=13000.0, equity=10000.0, target_lev=1.0))  # 30%, must fire
    msgs = [m for _, m in tg.sent]
    assert any("Gross drift" in m and "1.30x" in m for m in msgs)


def test_degraded_universe_warn():
    tg = FakeTelegram(["warning"])
    op = Operator(tg)
    _run(op.warn_degraded_universe(longs_avail=5, shorts_avail=2, longs_k=5, shorts_k=5))
    assert any("Universe degraded" in m and "shorts" in m for _, m in tg.sent)
    # not enough longs and enough shorts -> still fires only once per state via dedup
    tg.sent.clear()
    _run(op.warn_degraded_universe(longs_avail=5, shorts_avail=2, longs_k=5, shorts_k=5))
    assert tg.sent == [], "identical universe state should be deduped"


def test_position_mismatch_warn():
    tg = FakeTelegram(["warning"])
    op = Operator(tg)
    _run(op.warn_position_mismatch(expected=10, actual=11))
    assert any("Position count mismatch" in m for _, m in tg.sent)
    _run(op.warn_position_mismatch(expected=10, actual=10))  # equal → silent
    assert sum(1 for s, _ in tg.sent if s == "warning") == 1


def test_severity_filter_respected():
    tg = FakeTelegram(["rotation"])  # warning NOT in filter
    op = Operator(tg, OperatorConfig(gross_drift_warn_pct=1.0))
    _run(op.warn_gross_drift(gross=15000.0, equity=10000.0, target_lev=1.0))
    assert tg.sent == [], "severity not in filter list should suppress send"
