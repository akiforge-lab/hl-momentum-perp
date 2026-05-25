"""hl-momentum-perp — dry-run orchestrator.

Hard safety properties of this entrypoint:
  * Aborts unless execution.mode == "dry_run".
  * Never imports or uses a private key. No live execution path exists.
  * Calls only the public Hyperliquid /info endpoint for market & user data.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.exchange.candle_cache import CandleCache
from src.exchange.hl_info_client import HLInfoClient
from src.execution.dry_run_engine import DryRunConfig, DryRunEngine
from src.execution.intent_builder import diff_to_intents
from src.notify.operator import Operator, OperatorConfig
from src.notify.telegram import TelegramNotifier
from src.portfolio.account_state import AccountState
from src.portfolio.basket import build_basket, scale_basket_to_caps
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskConfig, RiskManager
from src.signal.momentum import rank, score_symbol
from src.signal.universe import candle_freshness_ok, filter_universe, parse_meta_ctx
from src.state.state_store import StateStore
from src.storage.trade_log import JsonlLog
from src.utils import logger as _log
from src.utils.time import next_daily_reset, utcnow


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def enforce_safety_gates(cfg: dict) -> None:
    mode = (cfg.get("execution", {}) or {}).get("mode")
    if mode != "dry_run":
        sys.stderr.write(f"FATAL: execution.mode must be 'dry_run' in v1 (got {mode!r}).\n")
        sys.exit(2)
    # Refuse to start if a private-key env var has been smuggled in.
    for forbidden in ("HL_PRIVATE_KEY", "HYPERLIQUID_PRIVATE_KEY", "PRIVATE_KEY"):
        if os.getenv(forbidden):
            sys.stderr.write(f"FATAL: {forbidden} is set in env. v1 forbids signing material.\n")
            sys.exit(2)


async def fetch_candle_map(
    info: HLInfoClient, symbols: list[str], interval: str, lookback_bars: int,
    *, cache: CandleCache | None, concurrency: int,
) -> dict[str, list[dict]]:
    """Fetch candles for many symbols, using cache where fresh and bounding
    concurrent /info calls. Logs a single summary line, not per-symbol noise."""
    bar_ms = 86_400_000 if interval == "1d" else 3_600_000  # 1d or 1h
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - bar_ms * (lookback_bars + 5)

    out: dict[str, list[dict]] = {}
    needs_fetch: list[str] = []

    if cache is not None:
        for sym in symbols:
            cached = cache.get(sym, interval)
            if cached is not None:
                out[sym] = cached
            else:
                needs_fetch.append(sym)
    else:
        needs_fetch = list(symbols)

    fetched = 0
    failed = 0
    if needs_fetch:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(sym: str) -> None:
            nonlocal fetched, failed
            async with sem:
                try:
                    candles = await info.candles(sym, interval, start_ms, end_ms)
                    out[sym] = candles
                    if cache is not None:
                        cache.put(sym, interval, candles)
                    fetched += 1
                except Exception as e:
                    out[sym] = []
                    failed += 1
                    log.warning("candle fetch failed", extra={"symbol": sym, "err": repr(e)})

        await asyncio.gather(*(_one(s) for s in needs_fetch))

    log.info("candles", extra={
        "requested": len(symbols),
        "from_cache": len(symbols) - len(needs_fetch),
        "fetched": fetched, "failed": failed, "concurrency": concurrency,
    })
    return out


async def one_cycle(
    cfg: dict, info: HLInfoClient, account: AccountState, risk: RiskManager,
    dry_engine: DryRunEngine, notifier: TelegramNotifier, jsonl: JsonlLog,
    store: StateStore, candle_cache: CandleCache | None, operator: Operator,
) -> None:
    t0 = time.time()
    # Single fetch per cycle for global market data (meta + per-asset context).
    # allMids is NOT called separately — metaAndAssetCtxs already includes mids.
    meta, ctxs = await info.meta_and_asset_ctxs()
    entries = parse_meta_ctx(meta, ctxs)
    u_cfg = cfg["universe"]
    eligible = filter_universe(
        entries,
        include=u_cfg.get("include") or [],
        exclude=u_cfg.get("exclude") or [],
        min_24h_notional_usdc=float(u_cfg["min_24h_notional_usdc"]),
    )
    log.info("universe", extra={"total": len(entries), "eligible": len(eligible)})

    symbols = [e.name for e in eligible]
    cc_cfg = cfg.get("candle_cache", {}) or {}
    candle_map = await fetch_candle_map(
        info, symbols, u_cfg["candle_interval"], int(u_cfg["candle_lookback"]),
        cache=candle_cache,
        concurrency=int(cc_cfg.get("fetch_concurrency", 4)),
    )

    mids = {e.name: e.mid for e in eligible}
    max_lev = {e.name: e.max_leverage for e in eligible}
    # HL `funding` is hourly fractional rate; convert to bps/hr
    funding_bps_hr = {e.name: e.funding * 1e4 for e in eligible}

    s_cfg = cfg["signal"]
    scores = []
    fresh_dropped = 0
    for e in eligible:
        cs = candle_map.get(e.name) or []
        if not candle_freshness_ok(cs, int(u_cfg["stale_max_sec"])):
            fresh_dropped += 1
            continue
        sc = score_symbol(
            e.name, cs,
            window=int(s_cfg["regression_window"]),
            min_bars=int(u_cfg["min_bars"]),
            min_r2=float(s_cfg["min_r2"]),
            r2_weighted=bool(s_cfg["r2_weight_in_score"]),
        )
        if sc is not None:
            scores.append(sc)
    longs, shorts = rank(scores, score_floor_abs=float(s_cfg["score_floor_abs"]))
    log.info("signals", extra={
        "scored": len(scores), "longs": len(longs), "shorts": len(shorts),
        "fresh_dropped": fresh_dropped,
    })

    b_cfg = cfg["basket"]; p_cfg = cfg["portfolio"]
    raw_targets = build_basket(
        longs, shorts,
        longs_k=int(b_cfg["longs_k"]), shorts_k=int(b_cfg["shorts_k"]),
        weighting=b_cfg["weighting"],
        equity_usdc=account.equity_usdc,
        target_leverage=float(p_cfg["target_leverage"]),
        mode=b_cfg["mode"],
    )

    # Portfolio-level rescale to fit gross/net/leverage BEFORE per-intent gating.
    # Keeps long/short sleeve balance instead of one-sided rejections.
    targets, scale_info = scale_basket_to_caps(
        raw_targets,
        equity_usdc=account.equity_usdc,
        max_gross_x_equity=float(p_cfg["max_gross_x_equity"]),
        max_net_abs_x_equity=float(p_cfg["max_net_abs_x_equity"]),
        max_leverage=float(p_cfg["max_leverage"]),
    )
    if scale_info.get("scaled"):
        log.info("basket scaled to caps", extra=scale_info)
        jsonl.append("basket_scale", scale_info)

    intents = diff_to_intents(targets, account, mids, float(b_cfg["rebalance_drift_pct"]))

    market_data_age_sec = time.time() - t0  # very recent fetch; conservative
    decision = risk.gate(
        intents,
        account=account, mids=mids, max_leverages=max_lev,
        funding_bps_per_hr=funding_bps_hr,
        market_data_age_sec=market_data_age_sec,
    )

    for intent, reason in decision.rejected:
        jsonl.append("risk_reject", {"intent": intent.to_dict(), "reason": reason})
    for intent in decision.accepted:
        jsonl.append("proposal", {"intent": intent.to_dict()})

    fills = dry_engine.simulate(decision.accepted, account)
    for f in fills:
        jsonl.append("dry_fill", f.__dict__)

    if risk.check_daily_loss(account, mids):
        await operator.kill("daily loss limit hit")

    store.save(account=account, extra={"cooldown_until_ts": risk.cooldown_until_ts})

    # ---- operator notifications -----------------------------------------
    # rotation (only if any OPEN/CLOSE/FLIP fills happened — INCREASE/REDUCE are routine)
    await operator.rotation(fills, equity=account.equity_usdc,
                            day_pnl=account.realized_pnl_today)
    # risk rejects (aggregated, dedup'd; also feeds the repeated-loop detector)
    await operator.rejects(decision.rejected)
    # abnormal-condition warnings
    await operator.warn_degraded_universe(
        longs_avail=len(longs), shorts_avail=len(shorts),
        longs_k=int(b_cfg["longs_k"]), shorts_k=int(b_cfg["shorts_k"]),
    )
    await operator.warn_stale_candles(fresh_dropped=fresh_dropped)
    await operator.warn_gross_drift(
        gross=account.gross_notional(mids),
        equity=max(account.equity_usdc, 1e-9),
        target_lev=float(p_cfg["target_leverage"]),
    )
    expected_pos = len([t for t in targets if t.notional_usdc > 0])
    await operator.warn_position_mismatch(expected=expected_pos,
                                          actual=len(account.positions))
    missing_mids = [t.symbol for t in targets if mids.get(t.symbol, 0.0) <= 0]
    await operator.warn_missing_mids(missing=missing_mids)


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    cfg = load_config(args.config)
    enforce_safety_gates(cfg)

    log_cfg = cfg["logging"]
    _log.setup(level=log_cfg["level"], json_mode=bool(log_cfg["json"]),
               app_log_path=log_cfg["app_log_path"])
    global log
    log = _log.get("main")

    log.info("startup", extra={
        "mode": cfg["execution"]["mode"],
        "target_leverage": cfg["portfolio"]["target_leverage"],
        "max_leverage": cfg["portfolio"]["max_leverage"],
        "longs_k": cfg["basket"]["longs_k"],
        "shorts_k": cfg["basket"]["shorts_k"],
    })

    kill = KillSwitch(cfg["risk"]["kill_file_path"])
    kill.set_config_flag(bool(cfg["risk"]["global_kill_switch"]))

    r_cfg = cfg["risk"]; p_cfg = cfg["portfolio"]
    risk = RiskManager(RiskConfig(
        max_leverage=float(p_cfg["max_leverage"]),
        max_gross_x_equity=float(p_cfg["max_gross_x_equity"]),
        max_net_abs_x_equity=float(p_cfg["max_net_abs_x_equity"]),
        max_per_symbol_pct_of_equity=float(p_cfg["max_per_symbol_pct_of_equity"]),
        min_liq_distance_pct=float(r_cfg["min_liq_distance_pct"]),
        stale_data_sec=int(r_cfg["stale_data_sec"]),
        max_daily_loss_pct=float(r_cfg["max_daily_loss_pct"]),
        stop_loss_cooldown_min=int(r_cfg["stop_loss_cooldown_min"]),
        daily_loss_cooldown_min=int(r_cfg["daily_loss_cooldown_min"]),
        funding_skip_abs_bps_per_hr=float(r_cfg["funding_skip_abs_bps_per_hr"]),
    ), kill)

    dry_engine = DryRunEngine(DryRunConfig(
        slippage_bps=float(cfg["execution"]["assumed_slippage_bps"]),
        taker_bps=float(cfg["execution"]["assumed_taker_bps"]),
        maker_bps=float(cfg["execution"]["assumed_maker_bps"]),
    ))

    notifier = TelegramNotifier(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        severities=cfg["notify"]["telegram"]["severities"],
        rate_limit_sec=float(cfg["notify"]["telegram"]["rate_limit_sec"]),
    )
    op_cfg = (cfg["notify"].get("operator") or {})
    operator = Operator(notifier, OperatorConfig(
        dedup_window_sec=int(op_cfg.get("dedup_window_sec", 3600)),
        gross_drift_warn_pct=float(op_cfg.get("gross_drift_warn_pct", 25.0)),
        stale_candles_warn=int(op_cfg.get("stale_candles_warn", 5)),
        repeated_reject_cycles=int(op_cfg.get("repeated_reject_cycles", 3)),
        repeated_reject_window=int(op_cfg.get("repeated_reject_window", 5)),
    ))

    store = StateStore(cfg["state"]["path"])
    saved = store.load()
    if saved.get("account"):
        account = AccountState.from_dict(saved["account"])
    else:
        account = AccountState(
            equity_usdc=float(cfg["dry_run"]["starting_equity_usdc"]),
            day_start_equity_usdc=float(cfg["dry_run"]["starting_equity_usdc"]),
        )
    if account.day_start_equity_usdc <= 0:
        account.day_start_equity_usdc = account.equity_usdc

    jsonl = JsonlLog(cfg["logging"]["decisions_path"])
    info = HLInfoClient()

    cc_cfg = cfg.get("candle_cache", {}) or {}
    candle_cache = CandleCache(cc_cfg.get("dir", "data/candles"),
                               int(cc_cfg.get("ttl_sec", 21600))) if cc_cfg.get("enabled", True) else None

    try:
        # Optional: refresh equity from chain for the configured account
        addr = os.getenv("HL_ACCOUNT_ADDRESS")
        if addr and cfg["dry_run"]["use_account_equity_if_available"]:
            try:
                state = await info.clearinghouse_state(addr)
                acct_value = float(state.get("marginSummary", {}).get("accountValue", 0.0))
                if acct_value > 0:
                    log.info("using on-chain equity for dry-run sizing",
                             extra={"equity": acct_value})
                    account.equity_usdc = acct_value
                    if account.day_start_equity_usdc <= 0:
                        account.day_start_equity_usdc = acct_value
            except Exception as e:
                log.warning("could not read on-chain equity", extra={"err": repr(e)})

        # startup announcement (always sent, regardless of dedup)
        await operator.startup(
            mode=cfg["execution"]["mode"],
            target_leverage=float(cfg["portfolio"]["target_leverage"]),
            max_leverage=float(cfg["portfolio"]["max_leverage"]),
            longs_k=int(cfg["basket"]["longs_k"]),
            shorts_k=int(cfg["basket"]["shorts_k"]),
        )

        if args.once:
            await one_cycle(cfg, info, account, risk, dry_engine, notifier, jsonl, store, candle_cache, operator)
            return

        interval = int(cfg["loop"]["rebalance_interval_sec"])
        reset_at = next_daily_reset(int(cfg["risk"]["daily_reset_utc_hour"]))
        while True:
            try:
                await one_cycle(cfg, info, account, risk, dry_engine, notifier, jsonl, store, candle_cache, operator)
            except Exception as e:
                log.exception("cycle error", extra={"err": repr(e)})
                await operator.error("one_cycle", repr(e))
            # daily reset
            if utcnow() >= reset_at:
                account.day_start_equity_usdc = account.equity_usdc
                account.realized_pnl_today = 0.0
                reset_at = next_daily_reset(int(cfg["risk"]["daily_reset_utc_hour"]))
                log.info("daily reset", extra={"day_start_equity": account.equity_usdc})
            await asyncio.sleep(interval)
    finally:
        await info.close()
        await notifier.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
