from __future__ import annotations

from dataclasses import dataclass

from ..signal.momentum import MomentumScore


@dataclass
class TargetPosition:
    symbol: str
    side: str            # "LONG" | "SHORT"
    notional_usdc: float # always positive; side carries the sign
    score: float
    weight: float


def build_basket(
    longs: list[MomentumScore],
    shorts: list[MomentumScore],
    *,
    longs_k: int,
    shorts_k: int,
    weighting: str,
    equity_usdc: float,
    target_leverage: float,
    mode: str,
) -> list[TargetPosition]:
    """Equal-weight diversified L/S basket. Gross = equity * target_leverage,
    split evenly between sleeves. Each sleeve equal-weights its K picks."""
    longs_sel = longs[:longs_k]
    shorts_sel = shorts[:shorts_k]

    if not longs_sel and not shorts_sel:
        return []

    gross = max(0.0, equity_usdc) * max(0.0, target_leverage)
    per_side = gross / 2.0

    out: list[TargetPosition] = []

    def _alloc(sleeve: list[MomentumScore], side: str) -> None:
        if not sleeve:
            return
        if weighting == "score":
            total = sum(abs(s.score) for s in sleeve) or 1.0
            for s in sleeve:
                w = abs(s.score) / total
                out.append(TargetPosition(s.symbol, side, per_side * w, s.score, w))
        else:  # equal
            w = 1.0 / len(sleeve)
            for s in sleeve:
                out.append(TargetPosition(s.symbol, side, per_side * w, s.score, w))

    if mode == "paired":
        # Trim to symmetric length so each pair gets equal gross on both legs
        k = min(len(longs_sel), len(shorts_sel))
        longs_sel, shorts_sel = longs_sel[:k], shorts_sel[:k]

    _alloc(longs_sel, "LONG")
    _alloc(shorts_sel, "SHORT")
    return out


def signed_notional(target: TargetPosition) -> float:
    return target.notional_usdc if target.side == "LONG" else -target.notional_usdc


def scale_basket_to_caps(
    targets: list[TargetPosition],
    *,
    equity_usdc: float,
    max_gross_x_equity: float,
    max_net_abs_x_equity: float,
    max_leverage: float,
) -> tuple[list[TargetPosition], dict]:
    """Portfolio-level rescale so the basket fits gross / net / leverage caps
    BEFORE per-symbol risk gating. Preserves sleeve balance: scales sleeves
    proportionally rather than dropping intents."""
    if not targets or equity_usdc <= 0:
        return targets, {"scaled": False}

    longs = [t for t in targets if t.side == "LONG"]
    shorts = [t for t in targets if t.side == "SHORT"]
    gl = sum(t.notional_usdc for t in longs)
    gs = sum(t.notional_usdc for t in shorts)
    gross = gl + gs
    if gross <= 0:
        return targets, {"scaled": False}

    g_cap = min(max_gross_x_equity, max_leverage) * equity_usdc
    n_cap = max_net_abs_x_equity * equity_usdc

    # Step 1: uniform shrink to gross cap (preserves sleeve ratio → preserves net ratio)
    s_uniform = min(1.0, g_cap / gross) if gross > 0 else 1.0
    long_scale = s_uniform
    short_scale = s_uniform

    gl2, gs2 = gl * long_scale, gs * short_scale

    # Step 2: if |net| still too large, shrink the dominant sleeve further
    net = gl2 - gs2
    net_shrink_applied = False
    if abs(net) > n_cap:
        if gl2 > gs2:
            target_gl = gs2 + n_cap
            if gl2 > 0:
                long_scale *= target_gl / gl2
        else:
            target_gs = gl2 + n_cap
            if gs2 > 0:
                short_scale *= target_gs / gs2
        net_shrink_applied = True

    scaled = (long_scale < 1.0) or (short_scale < 1.0)
    new_targets: list[TargetPosition] = []
    for t in targets:
        scale = long_scale if t.side == "LONG" else short_scale
        new_targets.append(TargetPosition(
            symbol=t.symbol, side=t.side,
            notional_usdc=t.notional_usdc * scale,
            score=t.score, weight=t.weight * scale,
        ))

    new_gl = sum(t.notional_usdc for t in new_targets if t.side == "LONG")
    new_gs = sum(t.notional_usdc for t in new_targets if t.side == "SHORT")
    return new_targets, {
        "scaled": scaled,
        "long_scale": round(long_scale, 4),
        "short_scale": round(short_scale, 4),
        "net_shrink_applied": net_shrink_applied,
        "before": {"gross": round(gross, 2), "net": round(gl - gs, 2),
                   "long": round(gl, 2), "short": round(gs, 2)},
        "after": {"gross": round(new_gl + new_gs, 2), "net": round(new_gl - new_gs, 2),
                  "long": round(new_gl, 2), "short": round(new_gs, 2)},
        "caps": {"gross": round(g_cap, 2), "net_abs": round(n_cap, 2)},
    }
