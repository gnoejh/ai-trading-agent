"""The live volatility-scaled stop.

exit_eval measured it (vol x 2 +0.475%/trip finished vs +0.141% for the fixed
8%, stop-outs halved). What is pinned here is the wiring: the policy honours
an explicit stop and derives the target from it; the supervisor asks its
injected reader once per adoption and clamps; a failed or empty read means the
FIXED stop, never no stop; 0 keeps the fixed stop with no read at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading.config import load_config
from trading.risk.exits import ExitPolicy, PositionSupervisor


def _cfg(tmp_path, k=2.0):
    c = load_config()
    c.exits.state = str(tmp_path / "exits.json")
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    m = c.exits.markets["BINANCE"]
    c.exits.markets["BINANCE"] = m.model_copy(
        update={
            "vol_multiple": k,
            "vol_min_stop": 0.03,
            "vol_max_stop": 0.15,
            "stop_loss_pct": 0.08,
        }
    )
    return c


def test_policy_honours_an_explicit_stop_and_derives_the_target_from_it(tmp_path):
    p = ExitPolicy(_cfg(tmp_path), market="BINANCE")
    fixed = p.plan_for("X", 100.0, 1.0)
    tight = p.plan_for("X", 100.0, 1.0, stop_pct=0.04)
    assert fixed.stop == pytest.approx(92.0)
    assert tight.stop == pytest.approx(96.0)
    assert tight.target < fixed.target, "reward:risk is a guarantee: less risk, nearer target"


def _supervisor(tmp_path, vol_of, k=2.0):
    cfg = _cfg(tmp_path, k)
    state = SimpleNamespace(reconcile=lambda **kw: None)
    return PositionSupervisor(state, cfg, market="BINANCE", vol_of=vol_of)


def test_supervisor_scales_and_clamps(tmp_path):
    sup = _supervisor(tmp_path, vol_of=lambda s: {"CALM": 1.0, "WILD": 12.0, "MID": 3.0}[s])
    assert sup.stop_pct_for("CALM") == pytest.approx(0.03)  # 2x1% = 2% -> floor 3%
    assert sup.stop_pct_for("WILD") == pytest.approx(0.15)  # 2x12% = 24% -> cap 15%
    assert sup.stop_pct_for("MID") == pytest.approx(0.06)


def test_no_reading_means_the_fixed_stop_never_no_stop(tmp_path):
    assert _supervisor(tmp_path, vol_of=lambda s: None).stop_pct_for("X") is None

    def boom(s):
        raise RuntimeError("data plane down")

    assert _supervisor(tmp_path, vol_of=boom).stop_pct_for("X") is None


def test_zero_multiple_reads_nothing(tmp_path):
    calls = []

    def reader(s):
        calls.append(s)
        return 5.0

    assert _supervisor(tmp_path, vol_of=reader, k=0.0).stop_pct_for("X") is None
    assert calls == []


def test_vol_stops_are_on_where_they_were_measured_alone():
    """Binance and KR were each read on their own grid; US has no closed trips
    and keeps its fixed stop until it does."""
    cfg = load_config()
    assert cfg.exits.for_market("BINANCE").vol_multiple > 0
    assert cfg.exits.for_market("KR").vol_multiple > 0
    assert cfg.exits.for_market("US").vol_multiple == 0
