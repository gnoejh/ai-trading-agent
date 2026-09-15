"""The feature definitions the backtest validates and the live screen ships.

One definition for both is the whole point (see the module docstring), so
these pin the arithmetic on hand-built bars and, above all, that nothing after
bar i is read -- a lookahead here would be a six-month backtest of a feature
that peeks.
"""

from __future__ import annotations

import math

import pytest

from trading.agent.features import PATH_KEYS, path_features, regime, relative


def _bars(closes, quote_vol=100.0, taker_quote=55.0):
    """Klines with the given closes; high/low bracket the close by 1%."""
    out = []
    for n, c in enumerate(closes):
        out.append(
            [
                n * 3_600_000,  # open_time
                c,
                c * 1.01,  # high
                c * 0.99,  # low
                c,  # close
                1.0,
                0,
                quote_vol,  # quote_volume
                1,
                0.5,
                taker_quote,  # taker_quote
            ]
        )
    return out


def test_returns_at_every_horizon_from_a_linear_ramp():
    closes = [100.0 + n for n in range(200)]  # +1 per hour
    f = path_features(_bars(closes))
    last = closes[-1]
    assert f["ret_1h"] == pytest.approx((last / (last - 1) - 1) * 100, abs=1e-3)
    assert f["ret_24h"] == pytest.approx((last / (last - 24) - 1) * 100, abs=1e-3)
    assert f["ret_7d"] == pytest.approx((last / (last - 168) - 1) * 100, abs=1e-3)


def test_nothing_after_bar_i_is_read():
    """Feature at i must be identical whether or not the future exists."""
    closes = [100.0 + math.sin(n / 5) * 5 for n in range(300)]
    full = _bars(closes)
    truncated = full[:201]
    assert path_features(full, 200) == path_features(truncated, 200)


def test_range_and_distance_from_the_week():
    closes = [100.0] * 100 + [110.0] * 60 + [90.0] * 8  # week high 111.1, low 89.1, close 90
    f = path_features(_bars(closes))
    assert f["from_7d_high_pct"] < 0 and f["from_7d_low_pct"] > 0
    assert 0.0 <= f["range_pos_7d"] <= 0.1  # sitting near the week's low


def test_volume_surge_ratio_and_taker_share():
    bars = _bars([100.0] * 200, quote_vol=100.0, taker_quote=60.0)
    for b in bars[-24:]:
        b[7] = 300.0  # today 3x the daily average
        b[10] = 240.0
    f = path_features(bars)
    assert f["vol_ratio_24h"] == pytest.approx(300 * 24 / ((300 * 24 + 100 * 144) / 7), rel=1e-3)
    assert f["taker_share_24h"] == pytest.approx(0.8)


def test_too_little_history_yields_none_not_a_short_window():
    f = path_features(_bars([100.0] * 30))
    assert f["ret_24h"] is not None and f["ret_7d"] is None and f["vol_24h_pct"] is None
    assert set(PATH_KEYS) <= set(f)


def test_relative_strength_is_name_minus_benchmark():
    a = {"ret_24h": 5.0, "ret_7d": 10.0, "ret_1h": None, "ret_4h": 1.0, "ret_72h": 2.0}
    btc = {"ret_24h": 2.0, "ret_7d": 4.0, "ret_1h": 0.1, "ret_4h": 0.5, "ret_72h": 1.0}
    rs = relative(a, btc)
    assert rs["rs_24h"] == 3.0 and rs["rs_7d"] == 6.0 and rs["rs_1h"] is None


def test_regime_breadth_counts_the_cross_section():
    section = [{"ret_24h": 1.0}, {"ret_24h": -1.0}, {"ret_24h": 2.0}, {"ret_7d": 1.0}]
    r = regime({"ret_24h": 0.5, "ret_7d": 3.0, "vol_24h_pct": 2.0}, section)
    assert r["btc_ret_7d"] == 3.0
    assert r["breadth_24h"] == pytest.approx(2 / 3, abs=1e-3)
    assert r["breadth_7d"] == 1.0
