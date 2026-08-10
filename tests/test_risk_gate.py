"""Risk gate tests.

The gate is the only thing between a model and a live account, so these cover the
fail-closed paths as hard as the happy path.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trading.brokers.kiwoom.account import Snapshot
from trading.config import load_config
from trading.risk.gate import RiskGate, Side, TradeIntent

SYMBOL = "005930"


class FakeState:
    """Stands in for AccountState; records whether reconciliation was demanded."""

    def __init__(self, snapshot: Snapshot, *, fail: bool = False, pnl: float = 0.0):
        self.snapshot = snapshot
        self.fail = fail
        self.reconciled = 0
        self.client = self  # gate reaches through .client for the daily P/L call
        self.pnl = pnl

    def assert_reconciled(self) -> Snapshot:
        self.reconciled += 1
        if self.fail:
            raise RuntimeError("broker unreachable")
        return self.snapshot

    def call(self, api_id, body=None, **kw):
        pnl = self.pnl

        class Page:
            body = {"rlzt_pl": str(int(pnl))}  # noqa: RUF012 - test stub

        return Page()


def snapshot(*, cash=12_000_000, total=100_000_000, held_qty=100, held_value=5_000_000):
    return Snapshot(
        market="KR",
        taken_at=dt.datetime.now(dt.UTC),
        cash={"entr": str(cash)},
        positions={
            "tot_evlt_amt": str(total),
            "rows": [{"stk_cd": SYMBOL, "rmnd_qty": str(held_qty), "evlt_amt": str(held_value)}],
        },
        open_orders={},
    )


@pytest.fixture
def cfg(tmp_path):
    # Pin the limits rather than inheriting config.yaml: these tests assert gate
    # behaviour, and must not change meaning when the live caps are re-tuned.
    c = load_config()
    c.risk.kill_switch_file = str(tmp_path / "HALT")
    c.risk.max_order_value_krw = 10_000_000
    c.risk.max_position_pct = 0.0
    c.risk.max_daily_loss_krw = 0.0
    c.risk.max_daily_loss_pct = 0.0
    c.risk.max_orders_per_cycle = 10
    c.risk.max_orders_per_day = 100
    c.agent.market = "KR"
    return c


def gate(cfg, state):
    return RiskGate(state, cfg)


def buy(qty=10, price=70_000, **kw):
    return TradeIntent(
        market="KR", side=Side.BUY, symbol=SYMBOL, quantity=qty, limit_price=price, **kw
    )


def test_approves_a_sane_order(cfg):
    g = gate(cfg, FakeState(snapshot()))
    assert g.evaluate(buy()).approved


def test_gate_always_reconciles_before_deciding(cfg):
    state = FakeState(snapshot())
    gate(cfg, state).evaluate(buy())
    assert state.reconciled == 1, "must re-read the broker, never trust a cached snapshot"


def test_kill_switch_blocks_everything(cfg):
    g = gate(cfg, FakeState(snapshot()))
    g.halt("manual")
    v = g.evaluate(buy())
    assert not v.approved and "kill switch" in v.reasons[0]


def test_kill_switch_checked_before_broker_read(cfg):
    """Halting must work even when the broker is down."""
    state = FakeState(snapshot(), fail=True)
    g = gate(cfg, state)
    g.halt("manual")
    assert not g.evaluate(buy()).approved
    assert state.reconciled == 0


def test_unreadable_state_fails_closed(cfg):
    g = gate(cfg, FakeState(snapshot(), fail=True))
    v = g.evaluate(buy())
    assert not v.approved and any("state unavailable" in r for r in v.reasons)


def test_unpriceable_order_fails_closed(cfg):
    """A market order with no reference price cannot be bounded, so it is refused."""
    g = gate(cfg, FakeState(snapshot()))
    intent = TradeIntent(market="KR", side=Side.BUY, symbol=SYMBOL, quantity=10)
    v = g.evaluate(intent)
    assert not v.approved and any("order value is unknown" in r for r in v.reasons)


def test_order_value_cap(cfg):
    cfg.risk.max_order_value_krw = 500_000
    g = gate(cfg, FakeState(snapshot()))
    v = g.evaluate(buy(qty=100, price=70_000))  # 7,000,000
    assert not v.approved and any("max_order_value_krw" in r for r in v.reasons)


def test_position_concentration_cap(cfg):
    cfg.risk.max_position_pct = 0.10
    cfg.risk.max_order_value_krw = 100_000_000
    # already holding 5,000,000 of a 100,000,000 account; +7,000,000 -> 12%
    g = gate(cfg, FakeState(snapshot()))
    v = g.evaluate(buy(qty=100, price=70_000))
    assert not v.approved and any("max_position_pct" in r for r in v.reasons)


def test_missing_equity_fails_closed(cfg):
    """Only when neither equity source is readable does concentration go unenforced."""
    cfg.risk.max_position_pct = 0.10
    snap = snapshot()
    snap.positions.pop("tot_evlt_amt")
    snap.positions.pop("prsm_dpst_aset_amt", None)
    snap.cash.pop("entr")
    v = gate(cfg, FakeState(snap)).evaluate(buy())
    assert not v.approved and any("equity unavailable" in r for r in v.reasons)


def test_insufficient_cash(cfg):
    cfg.risk.max_order_value_krw = 100_000_000
    cfg.risk.max_position_pct = 0.0
    g = gate(cfg, FakeState(snapshot(cash=100_000)))
    v = g.evaluate(buy(qty=10, price=70_000))
    assert not v.approved and any("available cash" in r for r in v.reasons)


def test_cannot_sell_more_than_held(cfg):
    g = gate(cfg, FakeState(snapshot(held_qty=5)))
    v = g.evaluate(
        TradeIntent(market="KR", side=Side.SELL, symbol=SYMBOL, quantity=50, limit_price=70_000)
    )
    assert not v.approved and any("exceeds holding" in r for r in v.reasons)


def test_cannot_sell_what_is_not_held(cfg):
    snap = snapshot()
    snap.positions["rows"] = []
    v = gate(cfg, FakeState(snap)).evaluate(
        TradeIntent(market="KR", side=Side.SELL, symbol=SYMBOL, quantity=1, limit_price=70_000)
    )
    assert not v.approved and any("no holding" in r for r in v.reasons)


def test_daily_loss_limit_halts_trading(cfg):
    cfg.risk.max_daily_loss_krw = 500_000
    g = gate(cfg, FakeState(snapshot(), pnl=-600_000))
    v = g.evaluate(buy())
    assert not v.approved and any("daily realised loss" in r for r in v.reasons)


def test_negative_quantity_rejected(cfg):
    g = gate(cfg, FakeState(snapshot()))
    assert not g.evaluate(buy(qty=-5)).approved


def test_wrong_market_rejected(cfg):
    g = gate(cfg, FakeState(snapshot()))
    intent = buy()
    intent.market = "US"
    v = g.evaluate(intent)
    assert not v.approved and any("market" in r for r in v.reasons)


def test_disabling_risk_does_not_enable_trading(cfg):
    """`risk.enabled: false` must not be a bypass."""
    cfg.risk.enabled = False
    v = gate(cfg, FakeState(snapshot())).evaluate(buy())
    assert not v.approved


def test_per_cycle_cap_trims_approvals(cfg):
    cfg.risk.max_orders_per_cycle = 2
    g = gate(cfg, FakeState(snapshot()))
    verdicts = g.evaluate_all([buy(qty=1), buy(qty=1), buy(qty=1), buy(qty=1)])
    assert sum(v.approved for v in verdicts) == 2
    assert any("max_orders_per_cycle" in r for v in verdicts for r in v.reasons)


def test_daily_order_budget(cfg):
    cfg.risk.max_orders_per_day = 2
    g = gate(cfg, FakeState(snapshot()))
    g.record_sent()
    g.record_sent()
    v = g.evaluate(buy())
    assert not v.approved and any("orders today" in r for r in v.reasons)


def test_concentration_uses_total_equity_not_holdings(cfg):
    """The denominator must be account equity.

    With 4.8M equity but only 172k of holdings, using `tot_evlt_amt` would make a
    300k order read as 175% of the account and reject everything.
    """
    cfg.risk.max_position_pct = 0.10
    cfg.risk.max_order_value_krw = 1_000_000
    snap = snapshot(cash=1_845_784, total=172_600, held_qty=2, held_value=172_600)
    snap.positions["prsm_dpst_aset_amt"] = "4802733"
    v = gate(cfg, FakeState(snap)).evaluate(buy(qty=1, price=300_000))
    assert v.approved, v.reasons


def test_equity_falls_back_to_cash_plus_holdings(cfg):
    cfg.risk.max_position_pct = 0.10
    snap = snapshot(cash=4_000_000, total=800_000, held_value=0)
    snap.positions.pop("prsm_dpst_aset_amt", None)
    # equity = 4,000,000 + 800,000 = 4,800,000; 10% = 480,000
    assert gate(cfg, FakeState(snap)).evaluate(buy(qty=1, price=400_000)).approved
    assert not gate(cfg, FakeState(snap)).evaluate(buy(qty=1, price=600_000)).approved


def test_market_order_with_reference_price_is_priceable(cfg):
    """A market order carries no limit; the reference price must let the gate value it."""
    g = gate(cfg, FakeState(snapshot()))
    intent = TradeIntent(
        market="KR", side=Side.BUY, symbol=SYMBOL, quantity=1, reference_price=70_000
    )
    v = g.evaluate(intent)
    assert v.approved, v.reasons


def sell(qty=10, price=70_000, **kw):
    return TradeIntent(
        market="KR", side=Side.SELL, symbol=SYMBOL, quantity=qty, limit_price=price, **kw
    )


def test_halt_blocks_entries_but_never_exits(cfg):
    """A kill switch that also blocks selling would trap the account in its position."""
    g = gate(cfg, FakeState(snapshot()))
    g.halt("manual")
    assert not g.evaluate(buy()).approved
    assert g.evaluate(sell()).approved, "a halt must not strip the ability to reduce risk"


def test_sizing_cap_does_not_block_closing_a_large_position(cfg):
    """max_order_value_krw is an entry sizing control, not an exit restriction."""
    cfg.risk.max_order_value_krw = 300_000
    g = gate(cfg, FakeState(snapshot(held_qty=100)))
    assert not g.evaluate(buy(qty=10, price=70_000)).approved  # 700k entry blocked
    assert g.evaluate(sell(qty=10, price=70_000)).approved  # 700k exit allowed


def test_daily_order_budget_does_not_strand_a_position(cfg):
    cfg.risk.max_orders_per_day = 1
    g = gate(cfg, FakeState(snapshot()))
    g.record_sent()
    assert not g.evaluate(buy()).approved
    assert g.evaluate(sell()).approved


def test_per_cycle_cap_never_rations_exits(cfg):
    cfg.risk.max_orders_per_cycle = 1
    g = gate(cfg, FakeState(snapshot(held_qty=100)))
    verdicts = g.evaluate_all([sell(qty=1), sell(qty=1), sell(qty=1)])
    assert all(v.approved for v in verdicts)


def test_daily_loss_cap_as_percentage_works_in_any_currency(cfg):
    """`*_krw` limits are meaningless on a USDT venue; a fraction of equity is not."""
    cfg.risk.max_daily_loss_krw = 0.0
    cfg.risk.max_daily_loss_pct = 0.10
    snap = snapshot()
    snap.positions["prsm_dpst_aset_amt"] = "3136"  # a USDT account
    # 10% of 3,136 = 313.6; a 400 loss must breach it
    v = gate(cfg, FakeState(snap, pnl=-400)).evaluate(buy())
    assert not v.approved and any("daily realised loss" in r for r in v.reasons)
    v2 = gate(cfg, FakeState(snap, pnl=-100)).evaluate(buy())
    assert v2.approved, v2.reasons
