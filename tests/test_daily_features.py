"""Daily feature definitions for equities: horizons that mean what they say.

An hourly KR bar exists only in session, so the hourly definitions would
silently mean sessions, not hours. These pin the daily variant: horizons in
trading days, no lookahead, None for missing history, and a turnover surge
against the 20-session average.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from trading.agent.features import (
    DAILY_PATH_KEYS,
    daily_path_features,
    regime_daily,
    relative_daily,
    turnover_ratio_daily,
)


def _bars(closes):
    return [SimpleNamespace(high=c * 1.02, low=c * 0.98, close=c) for c in closes]


def test_returns_in_trading_days():
    closes = [100.0 + n for n in range(40)]
    f = daily_path_features(_bars(closes))
    last = closes[-1]
    assert f["ret_1d"] == pytest.approx((last / (last - 1) - 1) * 100, abs=1e-3)
    assert f["ret_5d"] == pytest.approx((last / (last - 5) - 1) * 100, abs=1e-3)
    assert f["ret_20d"] == pytest.approx((last / (last - 20) - 1) * 100, abs=1e-3)


def test_nothing_after_bar_i_is_read():
    closes = [100.0 + math.sin(n / 3) * 4 for n in range(60)]
    assert daily_path_features(_bars(closes), 30) == daily_path_features(_bars(closes[:31]), 30)


def test_range_over_the_month():
    closes = [100.0] * 15 + [120.0] * 4 + [95.0]  # 20d high 122.4, low 93.1, close 95
    f = daily_path_features(_bars(closes))
    assert f["from_20d_high_pct"] < 0 < f["from_20d_low_pct"]
    assert 0.0 <= f["range_pos_20d"] <= 0.1


def test_too_little_history_yields_none():
    f = daily_path_features(_bars([100.0] * 10))
    assert f["ret_5d"] is not None and f["ret_20d"] is None and f["vol_d_pct"] is None
    assert {k for k in DAILY_PATH_KEYS if k != "turnover_ratio_5d"} <= set(f)


def test_turnover_surge():
    t = [100.0] * 15 + [300.0] * 5
    assert turnover_ratio_daily(t) == pytest.approx(300 / ((100 * 15 + 300 * 5) / 20), rel=1e-3)
    assert turnover_ratio_daily([100.0] * 10) is None


def test_relative_and_regime():
    a = {"ret_1d": 1.0, "ret_3d": 2.0, "ret_5d": 3.0, "ret_20d": 4.0}
    b = {"ret_1d": 0.5, "ret_3d": 0.5, "ret_5d": 0.5, "ret_20d": 0.5, "vol_d_pct": 1.0}
    assert relative_daily(a, b)["rs_5d"] == 2.5
    r = regime_daily(b, [{"ret_5d": 1.0}, {"ret_5d": -1.0}, {"ret_20d": 2.0}])
    assert r["bench_ret_5d"] == 0.5 and r["breadth_5d"] == 0.5 and r["breadth_20d"] == 1.0
