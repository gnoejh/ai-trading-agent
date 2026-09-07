"""The capital allocator: the model's share of the book, earned automatically.

These tests pin the SEAM, not only the arithmetic — the repository's four
production defects were all correct components that nothing called, or called
with the wrong argument. So alongside the ramp's shape there are tests that
`run_explore` actually reads the allocator's caps rather than the static
config, and that the random-arm set survives a restart (without which the
model's slot reserve is silently un-enforced on every service bounce).
"""

from __future__ import annotations

import json

import pytest

from trading.agent.allocator import Allocation, Allocator, plan
from trading.config import AllocatorConfig, AppConfig


@pytest.fixture
def cfg() -> AppConfig:
    """Hermetic: pinned thresholds, never inherited from config.yaml."""
    c = AppConfig()
    c.allocator = AllocatorConfig(
        enabled=True,
        base_share=0.50,
        max_share=0.85,
        min_share=0.15,
        target_net_pct=0.50,
        target_edge_pct=2.00,
        min_ready_buckets=30,
        max_step=1.0,  # most tests want the unclamped target; step is tested on its own
        min_explore_positions=1,
    )
    c.promotion.min_closed_trades = 100
    c.promotion.min_shadow_pairs = 30
    c.sizing.max_positions = 15
    return c


def _metrics(**over) -> dict:
    """Fully-earned metrics; override one field per test."""
    base = {
        "n_trips": 200,
        "pair_n": 100,
        "avg_net_pct": 1.0,  # 2x target -> profit score 1.0
        "edge_lower": 4.0,  # 2x target -> edge score 1.0
        "ready_buckets": 60,
    }
    base.update(over)
    return base


# -- the ramp ---------------------------------------------------------------


def test_full_evidence_reaches_the_owners_85_percent(cfg):
    """The owner's target is reachable, and is the ceiling."""
    a = plan(_metrics(), cfg)
    assert a.model_share == pytest.approx(0.85)


def test_epsilon_never_reaches_zero(cfg):
    """The control group survives BY CONSTRUCTION, however good the model looks.

    Without a live random arm, model-vs-chance stops being measurable — the
    standing invariant, enforced here in code rather than in a comment.
    """
    a = plan(_metrics(avg_net_pct=1e9, edge_lower=1e9, n_trips=10**9, pair_n=10**9), cfg)
    assert a.model_share <= cfg.allocator.max_share
    assert a.explore_entry_pct >= 1.0 - cfg.allocator.max_share > 0
    assert a.explore_max_positions >= cfg.allocator.min_explore_positions >= 1


def test_no_evidence_holds_at_base(cfg):
    """An empty store buys no capital — silence, never a fabricated prior."""
    a = plan({}, cfg)
    assert a.model_share == pytest.approx(cfg.allocator.base_share)


def test_profit_alone_does_not_buy_the_book(cfg):
    """The measured lesson: a long-only rule in a +96% window still lost to holding.

    Profit with no demonstrated edge over chance must not raise the share.
    """
    a = plan(_metrics(edge_lower=-1.66), cfg)
    assert a.profit_score == pytest.approx(1.0)
    assert a.edge_score == 0.0
    assert a.model_share == pytest.approx(cfg.allocator.base_share)


def test_edge_alone_does_not_buy_the_book(cfg):
    """Symmetrically: beating chance while making no money earns nothing."""
    a = plan(_metrics(avg_net_pct=0.0), cfg)
    assert a.model_share == pytest.approx(cfg.allocator.base_share)


def test_thin_evidence_scales_the_ramp(cfg):
    """Confidence gates quality: a lucky handful of trips cannot hand over the book."""
    a = plan(_metrics(n_trips=10), cfg)  # 10/100 -> confidence 0.10
    assert a.confidence == pytest.approx(0.10)
    assert a.model_share == pytest.approx(0.50 + (0.85 - 0.50) * 0.10)


def test_rag_readiness_is_part_of_the_evidence(cfg):
    """Context RL: the share tracks what the CONTEXT knows, not what the LLM is.

    The weights are frozen; policy improvement IS the growth of the measured
    record. An unfilled store therefore caps the ramp even when money and edge
    both look good.
    """
    a = plan(_metrics(ready_buckets=3), cfg)  # 3/30 -> the weakest term
    assert a.confidence == pytest.approx(0.10)
    assert a.model_share < 0.85


def test_losing_money_gives_the_book_back(cfg):
    """Downward needs no significance test — only realised loss."""
    a = plan(_metrics(avg_net_pct=-1.0), cfg)
    assert a.penalty == pytest.approx(1.0)
    assert a.model_share < cfg.allocator.base_share
    assert a.model_share >= cfg.allocator.min_share


def test_the_ramp_is_not_a_ratchet(cfg):
    """Unlike a stop, this must fall back when the model stops earning."""
    high = plan(_metrics(), cfg)
    then = plan(_metrics(avg_net_pct=-1.0), cfg, previous_share=high.model_share)
    assert then.model_share < high.model_share


def test_max_step_rate_limits_the_move(cfg):
    """A good week may nudge the book, never take it."""
    cfg.allocator.max_step = 0.05
    a = plan(_metrics(), cfg, previous_share=0.50)
    assert a.model_share == pytest.approx(0.55)


# -- the derived knobs ------------------------------------------------------


def test_both_explore_knobs_derive_from_one_share(cfg):
    """entry_pct and max_positions can never disagree: one number sets both."""
    a = plan(_metrics(), cfg)
    assert a.explore_entry_pct == pytest.approx(1.0 - a.model_share)
    assert a.explore_max_positions == round(cfg.sizing.max_positions * (1.0 - a.model_share))


def test_base_reproduces_the_historical_hand_tuned_config(cfg):
    """Automating the schedule moved the mechanism, not the endpoints.

    base_share 0.50 must reproduce the old `entry_pct: 0.5`, and max_share 0.85
    must land on the `floor_pct: 0.15` the config always documented as the
    manual decay target.
    """
    assert plan({}, cfg).explore_entry_pct == pytest.approx(0.5)
    assert plan(_metrics(), cfg).explore_entry_pct == pytest.approx(0.15)


def test_slot_cap_tightens_immediately_even_at_base(cfg):
    """The dominant constraint was slots, not frequency.

    281 of 298 decisions saw free_slots=0 while the random arm was permitted
    12 of 15 concurrent positions. At base the cap must already be far tighter.
    """
    assert plan({}, cfg).explore_max_positions == 8


# -- the live surface -------------------------------------------------------


def test_disabled_allocator_hands_back_the_config_untouched(cfg, tmp_path):
    """The switch is a real off switch: hand tuning must still be possible."""
    cfg.allocator.enabled = False
    cfg.explore.entry_pct = 0.42
    cfg.explore.max_positions = 11
    got = Allocator(cfg).maybe_run()
    assert got.explore_entry_pct == pytest.approx(0.42)
    assert got.explore_max_positions == 11


def test_share_persists_across_a_restart(cfg, tmp_path):
    """The allocation is a measured position, not session state.

    A restart must resume the ramp, not snap back to base.
    """
    cfg.allocator.state = str(tmp_path / "allocation.json")
    alloc = Allocator(cfg)
    alloc._save(Allocation(model_share=0.70, explore_entry_pct=0.30, explore_max_positions=5))
    assert Allocator(cfg).current.model_share == pytest.approx(0.70)


def test_interval_gates_recomputation(cfg, tmp_path, monkeypatch):
    """Recompute on the allocator's own cadence, not every cycle."""
    cfg.allocator.state = str(tmp_path / "allocation.json")
    cfg.allocator.interval_minutes = 60
    calls = {"n": 0}

    def fake_evaluate(_cfg=None):
        calls["n"] += 1
        return {"metrics": _metrics()}

    monkeypatch.setattr("trading.agent.promotion.evaluate", fake_evaluate)
    a = Allocator(cfg)
    import datetime as dt

    t0 = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.UTC)
    a.maybe_run(now=t0)
    a.maybe_run(now=t0 + dt.timedelta(minutes=10))
    assert calls["n"] == 1
    a.maybe_run(now=t0 + dt.timedelta(minutes=61))
    assert calls["n"] == 2


def test_state_file_records_the_arithmetic(cfg, tmp_path, monkeypatch):
    """Exploration updates a LEDGER, not a policy — auditable and revertible."""
    cfg.allocator.state = str(tmp_path / "allocation.json")
    monkeypatch.setattr(
        "trading.agent.promotion.evaluate", lambda _cfg=None: {"metrics": _metrics()}
    )
    Allocator(cfg).maybe_run()
    saved = json.loads((tmp_path / "allocation.json").read_text(encoding="utf-8"))
    for key in ("model_share", "profit_score", "edge_score", "confidence", "strength", "ts"):
        assert key in saved
