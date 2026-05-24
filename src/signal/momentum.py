from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..utils.math import ols_slope_r2


@dataclass
class MomentumScore:
    symbol: str
    slope_ann_pct: float
    r2: float
    score: float
    bars_used: int


def _closes(candles: list[dict]) -> np.ndarray:
    closes = []
    for c in candles:
        v = c.get("c")
        if v is None:
            continue
        try:
            closes.append(float(v))
        except (TypeError, ValueError):
            continue
    return np.asarray(closes, dtype=float)


def score_symbol(
    symbol: str,
    candles: list[dict],
    *,
    window: int,
    min_bars: int,
    min_r2: float,
    r2_weighted: bool,
) -> MomentumScore | None:
    closes = _closes(candles)
    if closes.size < min_bars:
        return None
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        return None
    y = np.log(closes[-window:]) if closes.size >= window else np.log(closes)
    slope_per_bar, r2 = ols_slope_r2(y)
    # daily bars → annualized % return = slope * 365 * 100
    slope_ann_pct = slope_per_bar * 365.0 * 100.0
    if r2 < min_r2:
        return None
    score = slope_ann_pct * r2 if r2_weighted else slope_ann_pct
    return MomentumScore(
        symbol=symbol,
        slope_ann_pct=round(slope_ann_pct, 4),
        r2=round(r2, 4),
        score=round(score, 4),
        bars_used=int(y.size),
    )


def rank(scores: list[MomentumScore], *, score_floor_abs: float) -> tuple[list[MomentumScore], list[MomentumScore]]:
    """Return (longs_sorted_desc, shorts_sorted_asc) above the |score| floor."""
    eligible = [s for s in scores if abs(s.score) >= score_floor_abs]
    longs = sorted([s for s in eligible if s.score > 0], key=lambda s: s.score, reverse=True)
    shorts = sorted([s for s in eligible if s.score < 0], key=lambda s: s.score)
    return longs, shorts
