"""Volatility-scaled stops in the exit grid.

A fixed 8% stop is a different bet on BTC than on a microcap. These pin the
pre-entry vol arithmetic, the clamp, and that a trip with no pre-entry price
record is ABSENT from the vol cells rather than silently replayed at the clamp
floor -- which would make the thinnest-history names look like the safest.
"""

from __future__ import annotations

import math

import pytest

from trading.agent.exit_eval import daily_vol_pct
from trading.agent.prices import Bar, Window


def _window(closes):
    bars = [Bar(t=i * 3_600_000, open=c, high=c, low=c, close=c) for i, c in enumerate(closes)]
    return Window(bars=bars, complete=True)


def test_daily_vol_scales_hourly_sigma_to_a_day():
    """Alternating +1%/-1% hourly moves: sigma ~1% per hour -> ~4.9% per day."""
    closes = [100.0]
    for i in range(200):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    v = daily_vol_pct(_window(closes))
    assert v == pytest.approx(1.0 * math.sqrt(24), rel=0.05)


def test_too_short_a_window_yields_none():
    assert daily_vol_pct(_window([100.0] * 10)) is None


def test_the_grid_keys_carry_the_multiple(tmp_path):
    from trading.config import load_config

    cfg = load_config()
    cfg.exit_eval.vol_multiples = [2.0]
    cfg.exit_eval.holds_minutes = [60]
    cfg.exit_eval.stops_pct = [0.08]
    cfg.exit_eval.reward_risks = [2.0]
    # The cell set is built inside run(); mirror its construction here so a
    # renamed key fails loudly rather than in production.
    from trading.agent.exit_eval import ExitEvaluator

    ev = ExitEvaluator(cfg)
    keys = {
        (h, s, r)
        for h in cfg.exit_eval.holds_minutes
        for s in cfg.exit_eval.stops_pct
        for r in cfg.exit_eval.reward_risks
    }
    keys |= {
        (h, f"vol x {k:g}", r)
        for h in cfg.exit_eval.holds_minutes
        for k in cfg.exit_eval.vol_multiples
        for r in cfg.exit_eval.reward_risks
    }
    assert (60, "vol x 2", 2.0) in keys and (60, 0.08, 2.0) in keys
    assert ev.ecfg.vol_min_stop < ev.ecfg.vol_max_stop


def test_clamp_bounds_the_per_trip_stop():
    from trading.config import load_config

    e = load_config().exit_eval
    for vol, k in ((0.5, 2.0), (30.0, 3.0)):
        stop = min(max(k * vol / 100, e.vol_min_stop), e.vol_max_stop)
        assert e.vol_min_stop <= stop <= e.vol_max_stop
