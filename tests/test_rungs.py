"""Ladder tests.

The load-bearing claim: exit rungs sit above NET break-even, not above entry.
Laddering out from the entry price sells the first rung at a loss after costs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading.accounting.costs import CostLedger
from trading.brokers.binance.symbols import SymbolRules
from trading.config import load_config
from trading.risk.rungs import RungPlanner

PRICE = 65_000.0


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    return c


@pytest.fixture
def planner(cfg):
    return RungPlanner(cfg, CostLedger(cfg))


def btc(min_notional="5"):
    return SymbolRules(
        symbol="BTCUSDT",
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal(min_notional),
    )


# -- entries -----------------------------------------------------------------


def test_entry_ladder_steps_down_from_the_reference(planner):
    rungs = planner.entry_rungs(PRICE, 3120.0, btc(), "CRYPTO")
    assert len(rungs) == 4
    prices = [r.price for r in rungs]
    assert prices == sorted(prices, reverse=True), "each rung must be cheaper than the last"
    assert prices[0] == pytest.approx(PRICE, rel=1e-4)


def test_entry_ladder_spends_about_the_budget(planner):
    rungs = planner.entry_rungs(PRICE, 3120.0, btc(), "CRYPTO")
    total = sum(r.notional for r in rungs)
    assert total <= 3120.0
    assert total > 3120.0 * 0.99, "quantisation should not strand meaningful capital"


def test_entry_is_front_loaded(planner):
    """Flat weights leave most capital waiting on a fall that may never come."""
    rungs = planner.entry_rungs(PRICE, 3120.0, btc(), "CRYPTO")
    assert rungs[0].notional > rungs[-1].notional


def test_rungs_below_min_notional_are_dropped_not_redistributed(planner):
    """Fattening the survivors would quietly undo the laddering."""
    rungs = planner.entry_rungs(PRICE, 3120.0, btc(min_notional="900"), "CRYPTO")
    assert len(rungs) < 4
    assert all(r.notional >= 900 for r in rungs)


def test_tiny_budget_yields_no_ladder(planner):
    assert planner.entry_rungs(PRICE, 1.0, btc(), "CRYPTO") == []


def test_whole_share_venue_gets_integer_quantities(planner):
    rungs = planner.entry_rungs(70_000.0, 1_845_784.0, None, "KR")
    assert rungs and all(r.quantity == int(r.quantity) for r in rungs)


# -- exits -------------------------------------------------------------------


def test_every_exit_rung_clears_net_breakeven(planner, cfg):
    """The point of the whole module."""
    hurdle = CostLedger(cfg).breakeven_move_pct("CRYPTO")
    breakeven = PRICE * (1 + hurdle)
    rungs = planner.exit_rungs(breakeven, 0.0478, btc(), "CRYPTO")
    assert rungs
    for r in rungs:
        assert r.price > breakeven, f"{r} sells at a loss after costs"
        assert r.price > PRICE


def test_exit_rungs_step_up(planner):
    rungs = planner.exit_rungs(65_570.0, 0.0478, btc(), "CRYPTO")
    prices = [r.price for r in rungs]
    assert prices == sorted(prices)


def test_exit_ladder_sells_the_whole_position(planner):
    qty = 0.0478
    rungs = planner.exit_rungs(65_570.0, qty, btc(), "CRYPTO")
    assert sum(r.quantity for r in rungs) == pytest.approx(qty, abs=1e-5)


def test_exit_spacing_scales_with_the_venue_hurdle(planner, cfg):
    """A costlier venue must demand a wider gain before scaling out."""
    cheap = planner.exit_rungs(100.0, 100.0, None, "US")  # 0.130%
    dear = planner.exit_rungs(100.0, 100.0, None, "BSTOCKS")  # 0.600%
    assert dear[0].price > cheap[0].price


def test_no_rungs_for_an_empty_position(planner):
    assert planner.exit_rungs(65_570.0, 0.0, btc(), "CRYPTO") == []
