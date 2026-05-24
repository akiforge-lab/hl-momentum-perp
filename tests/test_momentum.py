import numpy as np

from src.signal.momentum import score_symbol, rank
from src.utils.math import ols_slope_r2


def _candles_from_closes(closes):
    return [{"c": c, "t": i * 86400000, "T": (i + 1) * 86400000 - 1} for i, c in enumerate(closes)]


def test_ols_slope_r2_perfect_line():
    y = np.arange(50, dtype=float)
    slope, r2 = ols_slope_r2(y)
    assert abs(slope - 1.0) < 1e-9
    assert r2 > 0.999


def test_score_uptrend_positive():
    closes = list(np.exp(np.linspace(0, 0.5, 150)))  # +50% log-return uptrend
    sc = score_symbol("FOO", _candles_from_closes(closes),
                      window=90, min_bars=100, min_r2=0.2, r2_weighted=True)
    assert sc is not None and sc.score > 0


def test_rank_separates_longs_shorts():
    from src.signal.momentum import MomentumScore
    scores = [
        MomentumScore("A", 100, 0.9, 90, 90),
        MomentumScore("B", -200, 0.8, -160, 90),
        MomentumScore("C", 10, 0.5, 5, 90),
    ]
    longs, shorts = rank(scores, score_floor_abs=50)
    assert [s.symbol for s in longs] == ["A"]
    assert [s.symbol for s in shorts] == ["B"]
