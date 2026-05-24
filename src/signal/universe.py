from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class UniverseEntry:
    name: str
    max_leverage: int
    sz_decimals: int
    day_ntl_vlm: float
    mid: float
    funding: float          # hourly funding rate (decimal)
    open_interest: float


def parse_meta_ctx(meta: dict, ctxs: list[dict]) -> list[UniverseEntry]:
    out: list[UniverseEntry] = []
    for item, ctx in zip(meta.get("universe", []), ctxs):
        if item.get("isDelisted"):
            continue
        try:
            out.append(UniverseEntry(
                name=item["name"],
                max_leverage=int(item.get("maxLeverage", 1)),
                sz_decimals=int(item.get("szDecimals", 2)),
                day_ntl_vlm=float(ctx.get("dayNtlVlm", 0.0)),
                mid=float(ctx.get("midPx") or ctx.get("markPx") or 0.0),
                funding=float(ctx.get("funding", 0.0)),
                open_interest=float(ctx.get("openInterest", 0.0)),
            ))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def filter_universe(
    entries: list[UniverseEntry],
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    min_24h_notional_usdc: float,
) -> list[UniverseEntry]:
    inc = set(include or [])
    exc = set(exclude or [])
    out = []
    for e in entries:
        if inc and e.name not in inc:
            continue
        if e.name in exc:
            continue
        if e.mid <= 0 or not math.isfinite(e.mid):
            continue
        if e.day_ntl_vlm < min_24h_notional_usdc:
            continue
        out.append(e)
    return out


def candle_freshness_ok(candles: list[dict], stale_max_sec: int) -> bool:
    if not candles:
        return False
    last = candles[-1]
    # HL candles use 't' (open ms) and 'T' (close ms)
    close_ms = int(last.get("T") or last.get("t") or 0)
    if close_ms <= 0:
        return False
    age = time.time() - close_ms / 1000.0
    return age <= stale_max_sec
