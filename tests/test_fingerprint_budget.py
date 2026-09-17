"""The decide fingerprint sees prices, and the API budget is per venue.

Both are seams found live on 2026-09-17:

* `skip_decide_if_unchanged` compared the ORDERED SYMBOL TUPLE. Under the
  deterministic `sample` ranker the KR menu was byte-identical all day and the
  model was asked once in 33 cycles (15/day before), the whole evening session
  included -- while every price on the menu moved. The fingerprint now buckets
  each name's price, so a cycle skips only when nothing moved.
* One shared daily ceiling starved the US session alone (the budget day is
  UTC; US is its tail). A per-venue reserve lowers every OTHER venue's ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.test_explore import POOL, ApproveAll, DummyTelegram, StubAdapter
from trading.accounting.costs import CostLedger
from trading.agent.loop import TradingAgent, menu_fingerprint
from trading.config import AccountingConfig, load_config
from trading.llm.client import Usage


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
    c.agent.tiers.second_opinion = ""
    c.fit.model = str(tmp_path / "no_model.json")
    c.allocator.state = str(tmp_path / "allocation.json")
    c.explore.enabled = False
    c.sizing.mode = "fixed_fraction"
    c.sizing.fraction = 0.04
    c.sizing.max_positions = 2
    # The seams under test, pinned rather than inherited from config.yaml.
    c.agent.skip_decide_if_unchanged = True
    c.agent.fingerprint_price_step_pct = 0.5
    c.accounting.max_api_krw_per_day = 0
    c.accounting.api_reserve_krw = {}
    return c


REPLY = (
    '{"intents": [], "best_candidate": {"symbol": "BBBUSDT", "confidence": 0.5}, "commentary": "c"}'
)


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def ask(self, prompt, *, system=None, tier=None):
        self.calls += 1
        return REPLY


class MovingMenu(StubAdapter):
    """The same names every cycle; prices scale by whatever the test sets."""

    def __init__(self):
        super().__init__()
        self.scale = 1.0

    def candidates(self, order_size=0.0):
        return [{**e, "price": e["price"] * self.scale} for e in POOL]

    def holdings(self, snapshot):
        return {}

    def prices(self, symbols):
        return {s: 1.0 for s in symbols}


def _agent(cfg, adapter):
    agent = TradingAgent(cfg, notifier=DummyTelegram(), broker="binance", adapter=adapter)
    agent.gate = ApproveAll()
    agent.llm = CountingLLM()
    return agent


def _skips(cfg):
    text = Path(cfg.agent.journal).read_text(encoding="utf-8")
    rows = [json.loads(x) for x in text.splitlines() if x.strip()]
    return [r for r in rows if r["kind"] == "cycle_skipped"]


# -- menu_fingerprint ---------------------------------------------------------


def test_same_names_moved_prices_is_a_different_menu():
    before = menu_fingerprint(POOL, 0.5)
    moved = menu_fingerprint([{**e, "price": e["price"] * 1.01} for e in POOL], 0.5)
    assert before != moved, "a 1% move on every name is new information"


def test_a_move_inside_the_step_is_the_same_menu():
    before = menu_fingerprint(POOL, 0.5)
    # Bucket boundaries are log-spaced, so a tiny move can cross one; use a
    # move an order of magnitude under the step and check it stays put.
    nudged = menu_fingerprint([{**e, "price": e["price"] * 1.0001} for e in POOL], 0.5)
    assert before == nudged


def test_step_zero_is_symbols_only():
    """The pre-2026-09-17 behaviour, one config line away."""
    a = menu_fingerprint(POOL, 0)
    b = menu_fingerprint([{**e, "price": e["price"] * 3} for e in POOL], 0)
    assert a == b == tuple((e["symbol"], None) for e in POOL)


def test_order_still_matters_and_a_missing_price_is_tolerated():
    reordered = menu_fingerprint(list(reversed(POOL)), 0.5)
    assert reordered != menu_fingerprint(POOL, 0.5)
    unpriced = [
        {"symbol": "XUSDT"},
        {"symbol": "YUSDT", "price": None},
        {"symbol": "Z", "price": "n/a"},
    ]
    assert menu_fingerprint(unpriced, 0.5) == (("XUSDT", None), ("YUSDT", None), ("Z", None))


# -- the seam in run_cycle ----------------------------------------------------


def test_frozen_menu_with_moving_prices_still_asks_the_model(cfg):
    """The KR failure: identical names, live prices. Two cycles, two decisions."""
    adapter = MovingMenu()
    agent = _agent(cfg, adapter)
    agent.run_cycle()
    adapter.scale = 1.02
    agent.run_cycle()
    assert agent.llm.calls == 2
    assert _skips(cfg) == []


def test_frozen_menu_with_frozen_prices_skips_and_says_why(cfg):
    adapter = MovingMenu()
    agent = _agent(cfg, adapter)
    agent.run_cycle()
    agent.run_cycle()
    assert agent.llm.calls == 1
    [skip] = _skips(cfg)
    assert skip["reason"] == "unchanged candidates"
    assert skip["detail"].startswith(f"{len(POOL)} names, no price moved")


def test_symbols_only_fingerprint_reproduces_the_old_skip(cfg):
    cfg.agent.fingerprint_price_step_pct = 0
    adapter = MovingMenu()
    agent = _agent(cfg, adapter)
    agent.run_cycle()
    adapter.scale = 1.5
    agent.run_cycle()
    assert agent.llm.calls == 1, "with step 0, moved prices are not a new menu"


# -- api_ceiling_for ----------------------------------------------------------


def test_reserve_lowers_every_other_venue_but_not_its_own():
    acc = AccountingConfig(max_api_krw_per_day=9000, api_reserve_krw={"US": 1500})
    assert acc.api_ceiling_for("BINANCE") == 7500
    assert acc.api_ceiling_for("KR") == 7500
    assert acc.api_ceiling_for("US") == 9000


def test_no_reserve_is_the_plain_budget_and_no_budget_stays_unlimited():
    assert AccountingConfig(max_api_krw_per_day=6000).api_ceiling_for("US") == 6000
    unlimited = AccountingConfig(max_api_krw_per_day=0, api_reserve_krw={"US": 1500})
    assert unlimited.api_ceiling_for("BINANCE") == 0, "0 means unlimited to the loop"


def test_reserves_that_swallow_the_budget_are_refused_at_load():
    # A 0 ceiling reads as UNLIMITED in the loop, so a reserve that leaves
    # nothing for the others would un-cap them. The config is refused instead.
    with pytest.raises(ValidationError, match="no ceiling"):
        AccountingConfig(max_api_krw_per_day=1000, api_reserve_krw={"US": 600, "KR": 400})
    # No budget at all is fine: nothing is capped, so nothing can be un-capped.
    AccountingConfig(max_api_krw_per_day=0, api_reserve_krw={"US": 1500})


def test_the_loop_stops_at_its_own_venues_ceiling(cfg):
    """Binance with 8,000 spent: over its 7,500 ceiling with the US reserve,
    under the 9,000 budget without it. The SAME spend, two outcomes."""
    cfg.accounting.max_api_krw_per_day = 9000
    cfg.accounting.api_reserve_krw = {"US": 1500}
    ledger = CostLedger(cfg)
    ledger.record_llm(
        Usage(
            model="m",
            provider="p",
            tier="fast",
            input_tokens=1,
            output_tokens=1,
            usd=8000 / cfg.llm.usd_krw,
        )
    )
    agent = _agent(cfg, MovingMenu())
    agent.run_cycle()
    assert agent.llm.calls == 0
    [skip] = _skips(cfg)
    assert skip["reason"] == "api budget" and skip["ceiling_krw"] == 7500

    cfg.accounting.api_reserve_krw = {}
    agent = _agent(cfg, MovingMenu())
    agent.run_cycle()
    assert agent.llm.calls == 1, "without a reserve for another venue, 8,000 < 9,000 decides"
