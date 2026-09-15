"""The regime gate: the random arm slows down in a BTC-down week.

Measured over six months of backtest cross-sections: the pool's 72h raw return
is +1.58% when the benchmark's trailing week is up and -0.40% when down, a
+1.99% difference with a CI excluding zero. What is pinned here is the wiring:
the roll is scaled by the multiplier only when the week is down, the regime is
journalled BEFORE the roll so it is on the record either way, a missing
reading gates nothing (no data must never mean no entries), and 1.0 is off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_explore import POOL, StubAdapter, make_agent, observation
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.exits.state = str(tmp_path / "exits.json")
    c.risk.kill_switch_file = str(tmp_path / "HALT")
    c.score.enabled = False
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.feature_replay_output = str(tmp_path / "feature_replay.json")
    c.allocator.state = str(tmp_path / "allocation.json")
    c.allocator.enabled = False
    c.explore.enabled = True
    c.explore.entry_pct = 1.0
    c.explore.max_positions = 4
    c.explore.entries_per_cycle = 1
    c.explore.seed = 7
    c.explore.books = []
    c.sizing.mode = "fixed_fraction"
    c.sizing.fraction = 0.04
    c.sizing.max_positions = 6
    return c


def _agent(cfg, btc_7d):
    adapter = StubAdapter(pool=POOL)
    adapter.screen.market_state = lambda: {"btc_ret_7d": btc_7d} if btc_7d is not None else {}
    return make_agent(cfg, adapter=adapter)


def _rolls(agent, n=40):
    """How many of n cycles the arm attempts an entry (no positions are kept)."""
    sent = 0
    for _ in range(n):
        sent += agent.run_explore(observation(), free_slots=3)
        agent._random_positions.clear() if hasattr(agent, "_random_positions") else None
    return sent


def test_a_down_week_scales_the_roll(cfg, tmp_path):
    cfg.explore.regime_down_multiplier = 0.25
    cfg.explore.entry_pct = 0.8
    up = _rolls(_agent(cfg, btc_7d=+3.0))
    cfg.explore.seed = 7
    down = _rolls(_agent(cfg, btc_7d=-3.0))
    assert down < up, "the down-week arm must attempt fewer entries"
    rows = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    regimes = [r for r in rows if r["kind"] == "regime"]
    assert regimes and regimes[-1]["gated"] is True
    assert regimes[-1]["entry_pct"] == pytest.approx(0.8 * 0.25)


def test_an_up_week_is_untouched(cfg, tmp_path):
    cfg.explore.regime_down_multiplier = 0.25
    cfg.explore.entry_pct = 0.8
    _agent(cfg, btc_7d=+0.1).run_explore(observation(), free_slots=3)
    row = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    regime = [r for r in row if r["kind"] == "regime"][-1]
    assert regime["gated"] is False and regime["entry_pct"] == pytest.approx(0.8)


def test_no_reading_gates_nothing(cfg, tmp_path):
    """A data hiccup must not silently halve the arm -- no reading, no gate."""
    cfg.explore.regime_down_multiplier = 0.25
    cfg.explore.entry_pct = 0.8
    _agent(cfg, btc_7d=None).run_explore(observation(), free_slots=3)
    row = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    regime = [r for r in row if r["kind"] == "regime"][-1]
    assert regime["gated"] is False and regime["btc_ret_7d"] is None


def test_one_point_zero_is_off(cfg, tmp_path):
    cfg.explore.regime_down_multiplier = 1.0
    cfg.explore.entry_pct = 0.8
    _agent(cfg, btc_7d=-9.0).run_explore(observation(), free_slots=3)
    row = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    regime = [r for r in row if r["kind"] == "regime"][-1]
    assert regime["gated"] is False and regime["entry_pct"] == pytest.approx(0.8)


def test_market_state_is_one_benchmark_call():
    from trading.brokers.binance.universe import BinanceScreen

    calls = []

    def call(name, params):
        calls.append(params["symbol"])
        c = 100.0
        rows = [[i * 3_600_000, c, c, c, c + i * 0.1, 1, 0, 100, 1, 0.5, 55] for i in range(200)]
        return SimpleNamespace(body={"rows": rows})

    cfg = load_config()
    screen = BinanceScreen(SimpleNamespace(call=call), SimpleNamespace(), cfg)
    state = screen.market_state()
    assert calls == [cfg.agent.screen.benchmark_symbol]
    assert state["btc_ret_7d"] > 0 and "btc_vol_24h_pct" in state
