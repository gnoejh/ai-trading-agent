"""The decide payload silently stopped containing the menu, and the account.

This is the 2026-08-30 defect a second time. Then, the raw positions snapshot
pushed the payload past a blind `json.dumps(...)[:20000]` and cut `trade_rules`
off the END; the fix moved the critical fields to the FRONT so truncation would
"eat detail, never the contract". What that left in the tail was `cash`,
`holdings` and `open_orders` -- and the tail of the candidate list.

Then the 09-17 features tripled the candidate block. Measured 2026-09-18 on the
live journal:

    BINANCE   payload 24,013 chars vs a 20,000 guard   -> 19 of 25 candidates,
              and cash / holdings / open_orders NEVER ARRIVED, from 09-16
    KR        payload 21,100                            -> 23 of 24, same loss
    US        payload 10,293                            -> fits

Zero of 173 decisions complained, because nothing in the system prompt names
the missing fields the way it named `trade_rules`. A blind slice on JSON also
cuts mid-token, so the model was parsing malformed input.

Two things are pinned here. (1) The payload FITS BY CONSTRUCTION: always valid
JSON, the account state and the contract always present, the menu the only
thing that gives. (2) The SHADOW draws from the menu the model actually saw --
its docstring has always claimed "the same shortlist the model saw", and while
the payload was being sliced that was false, giving the gate's control a wider
choice set than the arm under test.
"""

from __future__ import annotations

import json

import pytest

from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.exits.state = str(tmp_path / "exits.json")
    return c


class _Agent:
    """The two methods under test, with the config they read."""

    def __init__(self, cfg):
        from trading.agent.loop import TradingAgent

        self.acfg = cfg.agent
        self._fit_payload = TradingAgent._fit_payload.__get__(self)
        self._shadow_pick = TradingAgent._shadow_pick.__get__(self)
        self._managed_symbols = lambda holdings, prices: set()

    @property
    def _rng(self):
        import random

        return random.Random(7)


def _body(n_candidates: int, per_candidate_chars: int = 600) -> dict:
    """A payload shaped like the live one: contract first, account last."""
    filler = "x" * per_candidate_chars
    return {
        "trade_rules": {"stop_pct": 8.0, "target_pct": 17.8, "hold_minutes": 4320},
        "limits": {"note": "rendered"},
        "measured_record": {"buckets": ["..."]},
        "candidates": [{"symbol": f"C{i}USDT", "blob": filler} for i in range(n_candidates)],
        "cash": {"USDT": 1234.5},
        "holdings": {"BTCUSDT": {"quantity": 1.0}},
        "unmanaged_balances": 3,
        "open_orders": [],
    }


def test_the_payload_is_always_valid_json(cfg):
    """A blind string slice cuts mid-token. This must never produce one."""
    cfg.agent.max_payload_chars = 4000
    text = _Agent(cfg)._fit_payload(_body(40))
    json.loads(text)  # raises if the old slice behaviour came back


def test_the_account_state_survives_a_squeeze(cfg):
    """cash / holdings / open_orders are what the old tail-slice removed."""
    cfg.agent.max_payload_chars = 4000
    body = json.loads(_Agent(cfg)._fit_payload(_body(40)))
    assert body["cash"] == {"USDT": 1234.5}
    assert body["holdings"] == {"BTCUSDT": {"quantity": 1.0}}
    assert "open_orders" in body
    assert body["unmanaged_balances"] == 3


def test_the_contract_survives_a_squeeze(cfg):
    """The 08-30 lesson: truncation must never reach `trade_rules`."""
    cfg.agent.max_payload_chars = 4000
    body = json.loads(_Agent(cfg)._fit_payload(_body(40)))
    assert body["trade_rules"]["stop_pct"] == 8.0
    assert body["measured_record"] == {"buckets": ["..."]}


def test_the_menu_is_what_gives_and_it_gives_from_the_tail(cfg):
    """The screen orders the menu, so the lowest-ranked names go first."""
    cfg.agent.max_payload_chars = 4000
    body = json.loads(_Agent(cfg)._fit_payload(_body(40)))
    shown = [c["symbol"] for c in body["candidates"]]
    assert 0 < len(shown) < 40, "some menu must survive, and some must be cut"
    assert shown == [f"C{i}USDT" for i in range(len(shown))], "cut from the tail"


def test_a_payload_that_fits_is_untouched(cfg):
    cfg.agent.max_payload_chars = 32000
    body = json.loads(_Agent(cfg)._fit_payload(_body(5)))
    assert len(body["candidates"]) == 5


def test_zero_disables_the_ceiling(cfg):
    cfg.agent.max_payload_chars = 0
    body = json.loads(_Agent(cfg)._fit_payload(_body(40)))
    assert len(body["candidates"]) == 40


def test_the_live_ceiling_clears_the_live_payload(cfg):
    """A regression guard on the CONFIG, not the code.

    The measured Binance payload was 24,013 chars. If a future feature pushes
    it past the ceiling again the menu silently shrinks, which is graceful but
    still a loss -- so the shipped ceiling must keep real headroom over the
    largest payload this repo has measured.
    """
    assert cfg.agent.max_payload_chars >= 28000, (
        "the measured live payload is ~24,000 chars; leave headroom"
    )


# -- the control must not get a wider menu than the arm under test ----------


def test_the_shadow_draws_only_from_what_the_model_saw(cfg):
    """Its docstring has always claimed this. While the payload was sliced it
    was false, and the bias ran against the model in the gate's own criterion."""
    cfg.agent.max_payload_chars = 4000
    agent = _Agent(cfg)
    observation = {
        "candidates": [{"symbol": f"C{i}USDT"} for i in range(40)],
        "holdings": {},
        "prices": {},
    }
    shown = json.loads(agent._fit_payload(_body(40)))["candidates"]
    allowed = {c["symbol"] for c in shown}
    assert len(allowed) < 40, "the fixture must actually trim, or this proves nothing"

    for _ in range(50):
        assert agent._shadow_pick(observation) in allowed


def test_the_shadow_falls_back_to_the_full_menu_when_no_payload_was_built(cfg):
    """A cycle that never called decide has no `_menu_shown`; the full offered
    list is then the honest draw, not an empty one."""
    agent = _Agent(cfg)
    observation = {
        "candidates": [{"symbol": "AUSDT"}, {"symbol": "BUSDT"}],
        "holdings": {},
        "prices": {},
    }
    assert agent._shadow_pick(observation) in {"AUSDT", "BUSDT"}
