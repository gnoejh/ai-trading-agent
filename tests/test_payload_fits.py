"""The decide payload silently stopped containing the menu, and the account.

This is the 2026-08-30 defect a second time. Then, the raw positions snapshot
pushed the payload past a blind `json.dumps(...)[:20000]` and cut `trade_rules`
off the END; the fix moved the critical fields to the FRONT so truncation would
"eat detail, never the contract". What that left in the tail was `cash`,
`holdings`, `open_orders` -- and the tail of the candidate list.

Then the 09-17 features tripled the candidate block. Measured 2026-09-18 on the
live journal:

    BINANCE   payload 24,013 chars vs a 20,000 guard   -> 19 of 25 candidates,
              and cash / holdings / open_orders NEVER ARRIVED, from 09-16
    KR        payload 21,100                            -> 23 of 24, same loss
    US        payload 10,293                            -> fits

Zero of 173 decisions complained, because nothing in the system prompt names
the missing fields the way it named `trade_rules`. A blind slice on JSON also
cuts mid-token, so the model was parsing malformed input.

**The first fix trimmed the menu to fit, and that was half a fix** (owner, the
same day: incomplete input and output must be *solved*, not bounded). A trimmed
prompt still produces a decision row, a virtual pick and a paired observation --
a measurement taken through a prompt nobody can reconstruct, flowing into the
corpus that decides mainnet. This system exists to measure, so a decision made
on incomplete input is worse than no decision.

So an over-size payload is now REFUSED, and the refusal takes the same path an
incomplete reply already takes (`LLMNoAnswer` -> `decide_failed`): no decision
row, no observation, no pair, and the error on Telegram. With ~8,000 chars of
headroom it should never fire; if it does, that is a config fault to fix rather
than a condition to ride out.
"""

from __future__ import annotations

import json

import pytest

from trading.agent.loop import IncompletePayload
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


# -- a payload that fits goes whole -----------------------------------------


def test_a_payload_that_fits_is_sent_whole_and_is_valid_json(cfg):
    cfg.agent.max_payload_chars = 32000
    body = json.loads(_Agent(cfg)._fit_payload(_body(5)))
    assert len(body["candidates"]) == 5
    assert body["cash"] == {"USDT": 1234.5}
    assert body["holdings"] == {"BTCUSDT": {"quantity": 1.0}}
    assert body["trade_rules"]["stop_pct"] == 8.0


def test_zero_disables_the_ceiling(cfg):
    cfg.agent.max_payload_chars = 0
    body = json.loads(_Agent(cfg)._fit_payload(_body(40)))
    assert len(body["candidates"]) == 40


# -- a payload that does not fit is refused, not shortened ------------------


def test_an_oversize_payload_is_refused_rather_than_trimmed(cfg):
    """The half-fix trimmed the menu and asked anyway. That still contaminates
    the corpus with a decision taken on a prompt nobody can reconstruct."""
    cfg.agent.max_payload_chars = 4000
    with pytest.raises(IncompletePayload):
        _Agent(cfg)._fit_payload(_body(40))


def test_the_refusal_says_what_to_change(cfg):
    """It should never fire, so when it does it must be actionable rather than
    merely alarming."""
    cfg.agent.max_payload_chars = 4000
    with pytest.raises(IncompletePayload) as exc:
        _Agent(cfg)._fit_payload(_body(40))
    message = str(exc.value)
    assert "4000" in message, "the ceiling it hit"
    assert "40 candidates" in message, "the menu size that hit it"
    assert "max_payload_chars" in message, "the knob to turn"


def test_the_live_ceiling_clears_the_live_payload(cfg):
    """A regression guard on the CONFIG, not the code.

    The measured Binance payload was 24,013 chars. Since an over-size payload now
    REFUSES to decide, a ceiling set too close to the real payload does not
    degrade quietly -- it stops the venue deciding. Keep real headroom.
    """
    assert cfg.agent.max_payload_chars >= 28000, (
        "the measured live payload is ~24,000 chars; leave headroom"
    )


# -- the control must not get a wider menu than the arm under test ----------


def test_the_shadow_draws_only_from_what_was_sent(cfg):
    """`_shadow_pick`'s docstring has always claimed "the same shortlist the
    model saw". While the payload was being sliced that was false, and the bias
    ran against the model inside the gate's own blocking criterion."""
    cfg.agent.max_payload_chars = 32000
    agent = _Agent(cfg)
    sent = json.loads(agent._fit_payload(_body(5)))
    allowed = {c["symbol"] for c in sent["candidates"]}

    # The observation carries MORE names than the payload did.
    observation = {
        "candidates": [{"symbol": f"C{i}USDT"} for i in range(40)],
        "holdings": {},
        "prices": {},
    }
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


def test_a_refused_payload_leaves_no_stale_menu_behind(cfg):
    """After a refusal there IS no menu the model saw, so `_menu_shown` must not
    keep pointing at the previous cycle's."""
    agent = _Agent(cfg)
    cfg.agent.max_payload_chars = 32000
    agent._fit_payload(_body(5))
    assert agent._menu_shown == [f"C{i}USDT" for i in range(5)]

    cfg.agent.max_payload_chars = 4000
    with pytest.raises(IncompletePayload):
        agent._fit_payload(_body(40))
    assert agent._menu_shown is None


def test_the_ceiling_warns_before_it_refuses(cfg, caplog):
    """A refusal stops the venue deciding, so it must never be the first anyone
    hears of the payload growing. `measured_record` grows as buckets fill."""
    import logging

    agent = _Agent(cfg)
    body = _body(5)
    size = len(json.dumps(body))
    cfg.agent.max_payload_chars = int(size / 0.95)  # inside the last 10%
    with caplog.at_level(logging.WARNING):
        agent._fit_payload(body)
    assert any("within 10%" in r.getMessage() for r in caplog.records)

    caplog.clear()
    cfg.agent.max_payload_chars = size * 10  # comfortable
    with caplog.at_level(logging.WARNING):
        agent._fit_payload(body)
    assert not caplog.records, "a comfortable payload must stay quiet"
