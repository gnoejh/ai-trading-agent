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


def test_exit_policy_uses_market_hurdle(policy, cfg):
    """Binance must not silently use the KR default fee hurdle."""
    assert policy.hurdle == pytest.approx(CostLedger(cfg).breakeven_move_pct("KR"))
    assert ExitPolicy(cfg, CostLedger(cfg), market="BINANCE").hurdle == pytest.approx(
        CostLedger(cfg).breakeven_move_pct("BINANCE")
    )


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
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    assert "005930" in s.plans, "a position with no plan must still get a stop"


def test_drops_plans_the_broker_no_longer_reports(cfg):
    s = sup(cfg)
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    s.check({}, {})
    assert s.plans == {}, "broker is the source of truth on what exists"


def test_no_cost_basis_refused_and_logged_once_not_per_pass(cfg, caplog):
    """A holding with no known entry gets no plan, and the refusal is logged
    when the set changes -- the testnet seeds ~480 such balances, which was
    482 error lines on EVERY cycle."""
    s = sup(cfg)
    seeds = {f"SEED{i}USDT": {"quantity": 100, "cost_basis": 0} for i in range(5)}
    with caplog.at_level("WARNING", logger="trading.risk.exits"):
        s.check(seeds, {})
        assert s.plans == {}, "no entry price means no stop worth the name"
        first_pass = [r for r in caplog.records if "no cost basis" in r.message]
        assert len(first_pass) == 1, "one aggregate line, not one per holding"
        caplog.clear()
        s.check(seeds, {})
        assert not [r for r in caplog.records if "no cost basis" in r.message], (
            "an unchanged refused set must not re-log"
        )


def test_dust_plan_is_closed_not_alarmed_forever(cfg):
    """Live 2026-08-31: a quantized exit left 0.34 PROM (~$2, under the $5
    minNotional); its stop refired every cycle and every order was refused.
    A holding that cannot form a valid order is dust: the plan closes and the
    remainder is never re-adopted."""
    s = PositionSupervisor(FakeState(), cfg, is_dust=lambda sym, qty, price: qty * price < 5)
    s.check({"PROMUSDT": {"quantity": 300, "cost_basis": 6.9}}, {"PROMUSDT": 6.9})
    assert "PROMUSDT" in s.plans
    # The exit sold the quantized bulk; the broker now reports the remainder.
    s.check({"PROMUSDT": {"quantity": 0.34, "cost_basis": 6.9}}, {"PROMUSDT": 6.2})
    assert "PROMUSDT" not in s.plans, "a dust plan must close, not alarm forever"
    signals = s.check({"PROMUSDT": {"quantity": 0.34, "cost_basis": 6.9}}, {"PROMUSDT": 6.2})
    assert signals == [], "dust must not be re-adopted either"


def test_exit_intents_keep_fractional_quantities(cfg):
    """int(sig.quantity) truncated the 0.34-PROM exit to 0, and the gate then
    refused it as non-positive -- the position was unexitable by construction."""
    import inspect

    from trading.agent.loop import TradingAgent

    assert "int(sig.quantity)" not in inspect.getsource(TradingAgent.run_exits)


def test_follows_broker_quantity_but_keeps_the_stop(cfg):
    s = sup(cfg)
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    stop = s.plans["005930"].stop
    s.check({"005930": {"quantity": 4, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY})
    assert s.plans["005930"].quantity == 4
    assert s.plans["005930"].stop == stop


def test_missing_price_does_not_fabricate_a_stop_breach(cfg):
    s = sup(cfg)
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    assert s.check({"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {}) == []


def test_state_survives_a_restart(cfg):
    s = sup(cfg)
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    stop = s.plans["005930"].stop
    revived = sup(cfg)
    assert revived.plans["005930"].stop == stop, "stops must outlive the process"


def test_supervisor_emits_a_stop_exit(cfg):
    s = sup(cfg)
    s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}}, {"005930": ENTRY}
    )
    signals = s.check(
        {"005930": {"quantity": 10, "avg_price": ENTRY, "cost_basis": ENTRY}},
        {"005930": ENTRY * 0.9},
    )
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


def test_position_without_cost_basis_is_refused(cfg):
    """A stop derived from the CURRENT price trails the market down and never
    fires. TUTUSDT lost 13.7% against an 8% stop exactly this way, so a holding
    with no real entry price is now left unmanaged and loudly logged."""
    s = sup(cfg)
    s.check({"005930": {"quantity": 10, "avg_price": ENTRY}}, {"005930": ENTRY})
    assert "005930" not in s.plans


def test_exit_state_is_per_market(cfg):
    """Two services sharing one state file corrupted it, so no plan survived."""
    a = PositionSupervisor(FakeState(), cfg, market="BINANCE")
    b = PositionSupervisor(FakeState(), cfg, market="US")
    assert a.path != b.path


# -- wiring, not components --------------------------------------------------
#
# Every bug that reached production was a correct component that nothing called,
# or called with the wrong argument. Unit tests all passed. These assert the seams.


def test_run_exits_is_called_every_cycle(cfg, monkeypatch):
    """`run_exits` was defined and never invoked -- stops were inert in production
    for a full day while a comment in run_cycle asserted they had already run."""
    import inspect

    from trading.agent.loop import TradingAgent

    src = inspect.getsource(TradingAgent.run_cycle)
    assert "self.run_exits(" in src, "run_cycle must invoke run_exits"
    # ...and before the halt check, so a halt cannot strip stop protection.
    assert src.index("self.run_exits(") < src.index("self.gate.halted"), (
        "exits must run BEFORE the halt check"
    )


def test_binance_exits_use_the_binance_hurdle(cfg):
    """`hurdle` omitted the market and silently used the KR default, pricing every
    Binance exit at 0.280% instead of 0.600%."""
    from trading.risk.exits import ExitPolicy

    binance = ExitPolicy(cfg, market="BINANCE").hurdle
    kr = ExitPolicy(cfg, market="KR").hurdle
    assert binance != kr
    assert binance == pytest.approx(cfg.accounting.fees_for("BINANCE").round_trip_rate())
