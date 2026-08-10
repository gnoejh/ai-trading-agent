"""Exit-policy tests.

The premise under test: a position is not flat at its entry price, it is flat at
entry plus the round-trip cost. These assert that the exit levels honour that.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trading.accounting.costs import CostLedger
from trading.config import load_config
from trading.risk.exits import ExitPolicy, ExitReason, PositionSupervisor

ENTRY = 100_000.0


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.accounting.fees.commission_rate = 0.00015
    c.accounting.fees.sell_tax_rate = 0.0015
    c.accounting.fees.slippage_bps = 5
    c.exits.state = str(tmp_path / "exits.json")
    c.exits.stop_loss_pct = 0.03
    c.exits.target_hurdle_multiple = 4.0
    c.exits.trail_arm_hurdle_multiple = 2.0
    c.exits.trail_give_back = 0.4
    c.exits.max_hold_minutes = 360
    return c


@pytest.fixture
def policy(cfg):
    return ExitPolicy(cfg, CostLedger(cfg))


def test_breakeven_is_above_entry_by_the_round_trip(policy):
    """Selling at entry is a loss; the policy must know that."""
    be = policy.net_breakeven(ENTRY, 10)
    assert be == pytest.approx(ENTRY * 1.0028)
    assert be > ENTRY


def test_api_spend_raises_breakeven(policy):
    """Token cost is a real cost and belongs in the position's basis."""
    plain = policy.net_breakeven(ENTRY, 10)
    with_api = policy.net_breakeven(ENTRY, 10, api_share=1000.0)
    assert with_api == pytest.approx(plain + 100.0)  # 1000 KRW over 10 shares


def test_target_clears_the_hurdle_at_minimum(policy):
    """The hurdle sets the floor on the target; the reward:risk rule may widen it."""
    plan = policy.plan_for("005930", ENTRY, 10)
    hurdle_target = plan.net_breakeven * (1 + 4 * policy.hurdle)
    assert plan.target >= hurdle_target
    assert plan.target > plan.net_breakeven > ENTRY


def test_stop_fires(policy):
    plan = policy.plan_for("005930", ENTRY, 10)
    sig = policy.evaluate(plan, plan.stop - 1)
    assert sig and sig.reason is ExitReason.STOP


def test_target_fires(policy):
    plan = policy.plan_for("005930", ENTRY, 10)
    sig = policy.evaluate(plan, plan.target + 1)
    assert sig and sig.reason is ExitReason.TARGET


def test_no_signal_in_the_middle(policy):
    plan = policy.plan_for("005930", ENTRY, 10)
    assert policy.evaluate(plan, ENTRY) is None


def test_time_stop_closes_a_position_going_nowhere(policy):
    opened = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=7)).isoformat()
    plan = policy.plan_for("005930", ENTRY, 10, opened_at=opened)
    sig = policy.evaluate(plan, ENTRY)
    assert sig and sig.reason is ExitReason.TIME


def test_trail_does_not_arm_before_the_position_is_genuinely_ahead(policy):
    """Trailing from entry would stop out positions that merely covered costs."""
    plan = policy.plan_for("005930", ENTRY, 10)
    assert policy.trail_stop_for(plan, ENTRY) is None
    assert policy.trail_stop_for(plan, plan.net_breakeven * 1.001) is None


def test_trail_arms_once_far_enough_ahead(policy):
    plan = policy.plan_for("005930", ENTRY, 10)
    price = plan.net_breakeven * (1 + 3 * policy.hurdle)
    trail = policy.trail_stop_for(plan, price)
    assert trail is not None and plan.net_breakeven < trail < price


def test_stop_ratchets_up_and_never_down(policy):
    plan = policy.plan_for("005930", ENTRY, 10)
    original = plan.stop
    assert plan.tighten_stop(original + 500)
    assert plan.stop == original + 500
    assert not plan.tighten_stop(original - 5000), "a stop must never be widened"
    assert plan.stop == original + 500


# -- supervisor --------------------------------------------------------------


class FakeState:
    pass


def sup(cfg):
    return PositionSupervisor(FakeState(), cfg)


def test_adopts_an_unmanaged_holding(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    assert "005930" in s.plans, "a position with no plan must still get a stop"


def test_drops_plans_the_broker_no_longer_reports(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    s.check({}, {})
    assert s.plans == {}, "broker is the source of truth on what exists"


def test_follows_broker_quantity_but_keeps_the_stop(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    stop = s.plans["005930"].stop
    s.check({"005930": {"quantity": 4, "avg_price": ENTRY}}, {"005930": ENTRY})
    assert s.plans["005930"].quantity == 4
    assert s.plans["005930"].stop == stop


def test_missing_price_does_not_fabricate_a_stop_breach(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    assert s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {}) == []


def test_state_survives_a_restart(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    stop = s.plans["005930"].stop
    revived = sup(cfg)
    assert revived.plans["005930"].stop == stop, "stops must outlive the process"


def test_supervisor_emits_a_stop_exit(cfg):
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    signals = s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY * 0.9})
    assert len(signals) == 1 and signals[0].reason is ExitReason.STOP


def test_target_never_falls_below_the_minimum_reward_risk(policy, cfg):
    """A hurdle-only target can be far tighter than the stop; the floor prevents it."""
    plan = policy.plan_for("005930", ENTRY, 10)
    assert policy.reward_risk(plan) >= cfg.exits.min_reward_risk - 1e-9
    assert plan.target > plan.net_breakeven + (plan.net_breakeven - plan.stop)


def test_a_wider_stop_pushes_the_target_out_with_it(policy, cfg):
    """Risk and reward must scale together, or the ratio silently degrades."""
    tight = policy.plan_for("005930", ENTRY, 10)
    cfg.exits.stop_loss_pct = 0.06
    wide = policy.plan_for("005930", ENTRY, 10)
    assert wide.stop < tight.stop
    assert wide.target > tight.target
    assert policy.reward_risk(wide) >= cfg.exits.min_reward_risk - 1e-9
