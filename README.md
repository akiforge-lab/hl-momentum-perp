# hl-momentum-perp

Hyperliquid perpetual-futures momentum engine. Adapted from
[`luno-momentum-mm`](https://github.com/akiforge-lab/luno-momentum-mm) for
cross-margin long/short basket trading. **v1 is dry-run only.**

## Strategy

Pure price momentum on daily candles. Rank the HL perp universe by
`momentum_score = slope_ann_pct * r2` (same convention as the
[hyperliquid-momentum-scanner](https://github.com/akiforge-lab/hyperliquid-momentum-scanner)
`pair_momentum_diversified.csv` output). Build a diversified long/short basket:

- top-K positive-momentum symbols → LONG sleeve
- bottom-K negative-momentum symbols → SHORT sleeve
- equal-weight within each sleeve
- gross exposure = `target_leverage × equity`, split evenly between sleeves
- portfolio-level rescale fits gross / net / leverage caps **before** per-symbol gating, preserving sleeve balance

The result is a market-neutralish basket that uses cross-margin efficiently.

## Safety guarantees (v1)

This codebase **cannot** place live orders. The guarantees are structural, not
just configurable:

1. No signing code exists. `eth_account` is not a dependency. There is no `live_engine.py`.
2. `.env.example` ships no private-key field. Only `HL_ACCOUNT_ADDRESS` is used, for read-only Info queries.
3. `execution.mode` must equal `dry_run`; any other value aborts startup.
4. `execution.allow_live` is a second hard gate; v1 ignores it even when `true`.
5. `enforce_safety_gates()` refuses to start if any of `HL_PRIVATE_KEY` / `HYPERLIQUID_PRIVATE_KEY` / `PRIVATE_KEY` is in the env.
6. Kill switch with three triggers: config flag, `state/KILL` file, or risk breach (daily loss).
7. All risk gates evaluate signed exposure, so LONG and SHORT are symmetric.
8. Every "would-place" intent is appended to `logs/decisions.jsonl` and sent as a Telegram proposal (if configured).
9. The HL client only POSTs to `/info` (public read endpoint). No code path reaches `/exchange`.

## Risk controls

- `max_leverage`, `max_gross_x_equity`, `max_net_abs_x_equity`
- `max_per_symbol_pct_of_equity`
- `min_liq_distance_pct` (cross-margin liquidation distance)
- `stale_data_sec` market-data freshness guard
- `max_daily_loss_pct` with cooldown
- `funding_skip_abs_bps_per_hr` skips adverse-funding entries
- Global kill switch (config flag, `state/KILL` file, or auto-trigger)

## Layout

```
src/
  exchange/    Hyperliquid public Info client + candle cache (read-only)
  signal/      OLS momentum ranking + universe filters
  portfolio/   Basket construction, leverage-aware sizing, portfolio rescaler, account state
  risk/        Pre-trade gates, liquidation distance, kill switch
  execution/   OrderIntent + dry-run simulator (no live engine in v1)
  notify/      Telegram, OpenClaw
  state/       JSON snapshot persistence
  storage/     Append-only JSONL decision log
  utils/       Logging, time, math helpers
simulation/    Deterministic rebalance simulation (no network)
deploy/        systemd unit templates (system + --user) and cron template
scripts/       Example wrappers (host-specific wrappers are gitignored)
tests/         pytest unit tests
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill TELEGRAM_* and HL_ACCOUNT_ADDRESS optionally
```

## Run

One dry-run cycle and exit:

```bash
.venv/bin/python main.py --once
```

Continuous loop (interval set by `loop.rebalance_interval_sec`):

```bash
.venv/bin/python main.py
```

Local convenience wrapper (template; copy to `dryrun.local.sh` — gitignored):

```bash
cp scripts/dryrun.example.sh dryrun.local.sh && chmod +x dryrun.local.sh
./dryrun.local.sh --once
```

## Inspect

```bash
tail -f logs/decisions.jsonl       # every proposal / risk_reject / dry_fill / basket_scale row
tail -f logs/app.log               # structured app log
cat state/state.json | jq          # current positions, equity, day-PnL, cooldowns
```

## Rebalance simulation (no network)

Deterministic end-to-end test of rebalance behavior using synthetic momentum
scores. Covers: cold-start basket open, no-change cycle (verifies no churn and
no Telegram spam), small drift (below drift threshold), large resize, symbol
rotation, and side flip. Asserts equity coherence (`Δequity == realized_PnL`).

```bash
.venv/bin/python -m simulation.rebalance_sim
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Verified behavior

Confirmed on a live `--once` run against `api.hyperliquid.xyz/info`:

- Universe 183 perps → 62 eligible after vol/freshness/notional filters
- 5 LONG / 5 SHORT basket built (gross $10k, net ≈ $0 at 1× leverage)
- Warm-cache cycle: **1** /info call total (meta+ctxs); 0 candle fetches within 6h TTL
- No-change re-run: 0 intents, 0 fills, no Telegram message

## Deployment (not enabled by default)

Two systemd unit templates ship; **neither is installed**:

- `deploy/systemd/hl-momentum-perp.service` — system-level (root install)
- `deploy/systemd/user/hl-momentum-perp.service` — user-level (recommended; no root)

User-level install (still not started until you choose):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/hl-momentum-perp.service ~/.config/systemd/user/
systemctl --user daemon-reload
# When ready:
systemctl --user start hl-momentum-perp
systemctl --user enable hl-momentum-perp
loginctl enable-linger $USER     # survives logout
journalctl --user -u hl-momentum-perp -f
```

`deploy/cron/daily_report.cron` is a template for a daily summary job.

## Local-only files (gitignored)

- `.env`
- `.venv/`
- `data/` (candle cache)
- `logs/`
- `state/`
- `*.local.sh` (host-specific wrappers)

## Roadmap

- v1 (this): dry-run engine, Telegram proposals, full risk gates, rebalance simulation
- v2: enrich signals (intraday stress overlay, funding/OI tilt, tracked wallets)
- v3: live execution behind an explicit, separately-reviewed PR that adds the signing path
