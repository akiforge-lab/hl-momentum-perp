"""Deterministic operator-oriented notification layer.

Purpose: turn raw cycle data into concise, low-noise Telegram messages
suitable for mobile reading. NO LLM calls. NO third-party AI APIs.
Everything here is plain string formatting + per-event dedup.

Severities (mapped to TelegramNotifier filter list):
  startup       - process boot, mode banner
  rotation      - OPEN / CLOSE / FLIP fills happened
  risk_reject   - one or more rejects this cycle (aggregated, dedup'd)
  warning       - abnormal conditions (gross drift, universe degraded, etc.)
  error         - exception in cycle
  kill          - kill switch tripped
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from ..utils.logger import get
from .telegram import TelegramNotifier

log = get(__name__)


@dataclass
class OperatorConfig:
    dedup_window_sec: int = 3600                    # suppress identical warnings within this window
    gross_drift_warn_pct: float = 25.0              # |gross/equity - target_lev| / target_lev * 100 threshold
    stale_candles_warn: int = 5                     # warn when fresh_dropped exceeds this
    repeated_reject_cycles: int = 3                 # same (sym,reason) for >= this many consecutive cycles -> warn
    repeated_reject_window: int = 5                 # consider last N cycles


class Operator:
    def __init__(self, notifier: TelegramNotifier, cfg: OperatorConfig | None = None):
        self.n = notifier
        self.cfg = cfg or OperatorConfig()
        self._last_sent: dict[str, float] = {}
        # rolling history of (symbol, reason) sets per recent cycle
        self._reject_history: deque[set[tuple[str, str]]] = deque(maxlen=self.cfg.repeated_reject_window)

    # ---- dedup ----------------------------------------------------------

    def _allow(self, kind: str, key: str = "") -> bool:
        k = f"{kind}|{key}"
        now = time.time()
        last = self._last_sent.get(k, 0.0)
        if now - last < self.cfg.dedup_window_sec:
            return False
        self._last_sent[k] = now
        return True

    # ---- always-send events ---------------------------------------------

    async def startup(self, *, mode: str, target_leverage: float, max_leverage: float,
                      longs_k: int, shorts_k: int) -> None:
        msg = (
            f"🟢 *hl-momentum-perp* started\n"
            f"mode: `{mode}`  target_lev: `{target_leverage:.2f}x` (max `{max_leverage:.2f}x`)\n"
            f"basket: `{longs_k}L / {shorts_k}S`"
        )
        await self.n.send("startup", msg)

    async def error(self, where: str, exc_repr: str) -> None:
        msg = f"🔴 *error* in `{where}`: `{exc_repr[:200]}`"
        # error always sends; no dedup (rare)
        await self.n.send("error", msg)

    async def kill(self, reason: str) -> None:
        msg = f"⛔ *kill switch active*: {reason}"
        await self.n.send("kill", msg)

    # ---- rotation summary -----------------------------------------------

    async def rotation(self, fills: Iterable, *, equity: float, day_pnl: float) -> None:
        opens, closes, flips = [], [], []
        for f in fills:
            a = getattr(f, "action", None)
            tag = f"{f.side[0]} `{f.symbol}`"
            if a == "OPEN":
                opens.append(tag)
            elif a in ("CLOSE",):
                closes.append(tag)
            elif a == "FLIP":
                flips.append(tag)
            # INCREASE/REDUCE are routine size changes — don't spam
        if not (opens or closes or flips):
            return
        lines = [f"🔄 *Rotation*  equity: `${equity:,.2f}`  day_pnl: `{day_pnl:+.2f}`"]
        if closes: lines.append(f"  closed: {', '.join(closes)}")
        if opens:  lines.append(f"  opened: {', '.join(opens)}")
        if flips:  lines.append(f"  flipped: {', '.join(flips)}")
        await self.n.send("rotation", "\n".join(lines))

    # ---- risk rejects ---------------------------------------------------

    async def rejects(self, rejects: list[tuple]) -> None:
        """rejects is a list of (OrderIntent, reason_str)."""
        current: set[tuple[str, str]] = set()
        if rejects:
            # Group by reason for compact message; dedup per (symbol, reason_class).
            by_reason: dict[str, list[str]] = {}
            for intent, reason in rejects:
                reason_class = reason.split(":", 1)[0]
                current.add((intent.symbol, reason_class))
                by_reason.setdefault(reason_class, []).append(f"{intent.side[0]} `{intent.symbol}`")

            # Send a compact summary, dedup'd per reason_class (not per symbol)
            for reason_class, syms in by_reason.items():
                if not self._allow("reject", reason_class):
                    continue
                msg = f"⚠️ *Risk reject* ({reason_class}): {', '.join(syms[:8])}"
                if len(syms) > 8: msg += f" (+{len(syms)-8} more)"
                await self.n.send("risk_reject", msg)

        # Track for repeated-loop detection
        self._reject_history.append(current)
        await self._check_repeated_rejects()

    async def _check_repeated_rejects(self) -> None:
        if len(self._reject_history) < self.cfg.repeated_reject_cycles:
            return
        # Find (sym, reason) pairs present in EVERY one of the last N cycles
        recent = list(self._reject_history)[-self.cfg.repeated_reject_cycles:]
        if any(not c for c in recent):
            return
        persistent = set.intersection(*recent)
        for sym, reason in persistent:
            if self._allow("repeated_reject", f"{sym}|{reason}"):
                await self.n.send("warning",
                    f"⚠️ *Repeated reject*: `{sym}` keeps failing `{reason}` "
                    f"({self.cfg.repeated_reject_cycles} cycles in a row)")

    # ---- abnormal conditions --------------------------------------------

    async def warn_gross_drift(self, *, gross: float, equity: float, target_lev: float) -> None:
        if equity <= 0 or target_lev <= 0:
            return
        eff = gross / equity
        drift_pct = abs(eff - target_lev) / target_lev * 100.0
        if drift_pct < self.cfg.gross_drift_warn_pct:
            return
        if not self._allow("gross_drift", ""):
            return
        await self.n.send("warning",
            f"⚠️ *Gross drift* `{eff:.2f}x` vs target `{target_lev:.2f}x` "
            f"(`{drift_pct:.0f}%` off)")

    async def warn_degraded_universe(self, *, longs_avail: int, shorts_avail: int,
                                      longs_k: int, shorts_k: int) -> None:
        msgs = []
        if longs_avail < longs_k:
            msgs.append(f"only `{longs_avail}` eligible longs (need `{longs_k}`)")
        if shorts_avail < shorts_k:
            msgs.append(f"only `{shorts_avail}` eligible shorts (need `{shorts_k}`)")
        if not msgs:
            return
        key = f"L{longs_avail}/S{shorts_avail}"
        if not self._allow("degraded_universe", key):
            return
        await self.n.send("warning", "⚠️ *Universe degraded*: " + " — ".join(msgs))

    async def warn_stale_candles(self, *, fresh_dropped: int) -> None:
        if fresh_dropped < self.cfg.stale_candles_warn:
            return
        if not self._allow("stale_candles", ""):
            return
        await self.n.send("warning",
            f"⚠️ *Stale candles*: `{fresh_dropped}` symbols dropped on freshness")

    async def warn_position_mismatch(self, *, expected: int, actual: int) -> None:
        if expected == actual:
            return
        if not self._allow("position_mismatch", f"{expected}/{actual}"):
            return
        await self.n.send("warning",
            f"⚠️ *Position count mismatch*: expected `{expected}`, "
            f"have `{actual}` in state")

    async def warn_missing_mids(self, *, missing: list[str]) -> None:
        if not missing:
            return
        if not self._allow("missing_mids", ",".join(sorted(missing))[:64]):
            return
        await self.n.send("warning",
            f"⚠️ *Missing mids*: `{', '.join(missing[:8])}`" +
            (f" (+{len(missing)-8})" if len(missing) > 8 else ""))
