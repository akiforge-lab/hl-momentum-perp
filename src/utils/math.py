import numpy as np


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def bps(x: float) -> float:
    return x * 1e-4


def ols_slope_r2(y: np.ndarray) -> tuple[float, float]:
    """Fit y = a + b*x where x = 0..n-1. Returns (slope_per_bar, r2)."""
    n = len(y)
    if n < 3 or not np.all(np.isfinite(y)):
        return 0.0, 0.0
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx <= 0:
        return 0.0, 0.0
    sxy = float(((x - xm) * (y - ym)).sum())
    slope = sxy / sxx
    yhat = ym + slope * (x - xm)
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - ym) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, max(0.0, r2)
