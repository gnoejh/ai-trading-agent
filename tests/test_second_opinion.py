"""The second-opinion LLM: a stronger model measured, never obeyed.

`agent.tiers.second_opinion` names a tier that is asked the IDENTICAL question
on every decision. Only its `best_candidate` is kept, journalled as
`virtual_pick_deep` and scored as `arm_llm_<tier>` on the selector leaderboard
against the same shadow as every other arm. It answers "would a stronger model
select better" by measurement. The invariants that matter: the traded decision
comes from the decide tier ALONE; a failing second opinion cannot touch it; off
means one call, not two.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.test_explore import POOL, make_agent, make_scorer, observation
from trading.agent.scorer import ARM_PREFIX
from trading.config import load_config

FAST = (
    '{"intents": [], "best_candidate": {"symbol": "AAAUSDT", "confidence": 0.4},'
    ' "commentary": "fast declines"}'
)
DEEP = (
    '{"intents": [{"side": "BUY", "symbol": "BBBUSDT", "quantity": 0, "limit_price": null,'
    ' "reason": "deep would buy", "confidence": 0.9}],'
    ' "best_candidate": {"symbol": "BBBUSDT", "confidence": 0.7}, "commentary": "deep"}'
)
NL = chr(10)


@pytest.fixture
def cfg(tmp_path):
    """Same shape as test_explore's; each file owns its fixture (repo pattern).

    Pins what these tests assert on: the decide tier, and the second opinion
    per test -- a live config.yaml edit must never move a call count here.
    """
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.exits.state = str(tmp_path / "exits.json")
    c.risk.kill_switch_file = str(tmp_path / "HALT")
    c.score.enabled = False
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.allocator.state = str(tmp_path / "allocation.json")
    c.allocator.enabled = False
    c.explore.enabled = False
    c.agent.tiers.decide = "fast"
    c.agent.tiers.confidence_floor = 0.0  # never escalate: call counts are the subject
    return c


class _LLM:
    def __init__(self, replies, fail_tier=None):
        self.replies, self.fail_tier, self.calls = replies, fail_tier, []

    def ask(self, prompt, *, system=None, tier=None):
        self.calls.append(tier)
        if tier == self.fail_tier:
            raise RuntimeError("deep tier down")
        return self.replies[tier]


def _obs():
    o = observation(candidates=list(POOL))
    o["tradable"] = [e["symbol"] for e in POOL]
    return o


def test_the_traded_decision_comes_from_the_decide_tier_alone(cfg):
    """The deep reply proposes a BUY; the decision must still be the fast decline."""
    cfg.agent.tiers.decide = "fast"
    cfg.agent.tiers.second_opinion = "deep"
    agent = make_agent(cfg)
    agent.llm = _LLM({"fast": FAST, "deep": DEEP})
    intents, _c, best, conf = agent.decide(_obs())
    assert intents == [] and best == "AAAUSDT" and conf == 0.4
    assert agent.llm.calls == ["fast", "deep"]
    assert agent._second_opinion == ("BBBUSDT", 0.7)


def test_a_failing_second_opinion_cannot_touch_the_decision(cfg):
    cfg.agent.tiers.decide = "fast"
    cfg.agent.tiers.second_opinion = "deep"
    agent = make_agent(cfg)
    agent.llm = _LLM({"fast": FAST, "deep": DEEP}, fail_tier="deep")
    intents, _c, best, _conf = agent.decide(_obs())
    assert intents == [] and best == "AAAUSDT"
    assert agent._second_opinion == (None, None)


def test_off_means_one_call(cfg):
    cfg.agent.tiers.decide = "fast"
    cfg.agent.tiers.second_opinion = ""
    agent = make_agent(cfg)
    agent.llm = _LLM({"fast": FAST})
    agent.decide(_obs())
    assert agent.llm.calls == ["fast"]


def _journal(tmp_path, **extra):
    decision = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "kind": "decision",
        "candidates": POOL,
        "shadow_random": "AAAUSDT",
        "virtual_pick": "AAAUSDT",
        "virtual_confidence": 0.4,
        "verdicts": [],
        **extra,
    }
    (tmp_path / "journal.jsonl").write_text(json.dumps(decision) + NL, encoding="utf-8")


def test_the_pick_is_journalled_and_scored_as_its_own_arm(cfg, tmp_path):
    """End to end: a decision row carrying the second opinion opens `arm_llm_deep`."""
    cfg.score.arms = []
    _journal(
        tmp_path,
        second_opinion_tier="deep",
        virtual_pick_deep="BBBUSDT",
        virtual_confidence_deep=0.7,
    )
    stats = make_scorer(cfg, pool=[]).run_once()
    assert stats["opened_journal"] == 3, "model + shadow + the second opinion"
    opened = [
        json.loads(line) for line in (tmp_path / "observations.jsonl").read_text().splitlines()
    ]
    rows = {(r["source"], r["symbol"]): r for r in opened if r["kind"] == "open"}
    deep = rows[(f"{ARM_PREFIX}llm_deep", "BBBUSDT")]
    assert deep.get("confidence") == 0.7, "its stated confidence travels with the pick"


def test_a_row_without_a_second_opinion_opens_nothing_extra(cfg, tmp_path):
    """Rows from before the feature, or with it switched off, are unchanged."""
    cfg.score.arms = []
    _journal(tmp_path)
    assert make_scorer(cfg, pool=[]).run_once()["opened_journal"] == 2
