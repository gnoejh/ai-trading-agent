"""Neither the model's input nor its output may be truncated (owner, 2026-09-19).

Stated after the decide payload was found losing 6 of 25 candidates and the
entire account state to a blind `json.dumps(...)[:20000]`. The principle is
broader than that bug, and the audit it prompted found four more violations:

  * `replay.py` carried the identical `[:20000]` slice, feeding the gate's
    "backtest prior" line from a silently shortened menu.
  * `LLMClient._truncated` returned None -- "no problem" -- for any reply with
    non-empty content, so a reply CUT OFF at the token cap was treated as a
    complete answer. `finish_reason == "length"` was never consulted unless the
    content was empty. Measured on the ledger: 30 of 1,724 v4-flash calls (1.7%)
    ended at the 32,768 cap.
  * `commentary` was sliced at 1,000 chars and each intent's `reason` at 500,
    into the append-only journal that is the permanent record of what the model
    said -- and the thing every later diagnosis reads.

The rule these pin: input is never silently shortened, output is never silently
shortened, and a truncation that does happen is LOUD. The failure this prevents
is the one the repo has now paid for three times -- silence that is
indistinguishable from a decision.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from trading.agent.loop import TradingAgent, _keep
from trading.config import load_config
from trading.llm.client import LLMClient


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    return c


# -- output: a reply cut off at the token cap is not an answer --------------


def _choice(content, finish, reasoning=""):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, reasoning_content=reasoning),
        finish_reason=finish,
    )


def test_a_reply_cut_off_at_the_cap_is_reported_even_with_content():
    """The hole: non-empty content returned None before finish_reason was read.

    A 30,000-char reply that stops mid-JSON parses to nothing, `_parse` finds no
    JSON, and the cycle reports 0 intents -- which the journal cannot tell apart
    from a considered decision not to trade.
    """
    c = _choice('{"intents": [{"symbol": "BTCUS', "length")
    problem = LLMClient._truncated(c.message, c)
    assert problem is not None
    assert "cut off" in problem


def test_a_complete_reply_is_not_flagged():
    c = _choice('{"intents": []}', "stop")
    assert LLMClient._truncated(c.message, c) is None


def test_empty_content_after_reasoning_is_still_reported():
    """The original trap (2026-08-10, 2026-08-31) must stay caught."""
    c = _choice("", "length", reasoning="x" * 29_751)
    problem = LLMClient._truncated(c.message, c)
    assert problem is not None
    assert "29751" in problem.replace(",", "")


def test_empty_content_with_no_reasoning_is_reported():
    c = _choice("   ", "stop")
    assert LLMClient._truncated(c.message, c) is not None


# -- output: the model's own words reach the journal whole ------------------


def test_the_models_words_are_kept_whole(cfg):
    text = "a considered explanation " * 100  # 2,500 chars, over the old 1,000
    assert _keep(text, "commentary", cfg.agent.max_model_text_chars) == text


def test_a_cap_that_bites_says_so(caplog):
    with caplog.at_level(logging.WARNING):
        out = _keep("x" * 50, "commentary", 10)
    assert out == "x" * 10
    assert any("truncated" in r.getMessage() for r in caplog.records), (
        "a truncation must never be silent -- that is the whole rule"
    )


def test_zero_disables_the_output_cap():
    text = "y" * 100_000
    assert _keep(text, "commentary", 0) == text


def test_parse_keeps_a_long_commentary_and_a_long_reason(cfg):
    """End to end through `_parse`, which is what actually writes the journal."""
    import json

    reason = "because " * 200  # 1,600 chars, over the old 500
    commentary = "reasoning " * 300  # 3,000 chars, over the old 1,000
    raw = json.dumps(
        {
            "intents": [
                {
                    "market": "BINANCE",
                    "side": "BUY",
                    "symbol": "AAAUSDT",
                    "quantity": 1,
                    "reason": reason,
                    "confidence": 0.5,
                }
            ],
            "best_candidate": {"symbol": "AAAUSDT", "confidence": 0.5},
            "commentary": commentary,
        }
    )
    stub = SimpleNamespace(market="BINANCE", acfg=cfg.agent)
    intents, got_commentary, _best, _conf = TradingAgent._parse(
        stub, raw, {"AAAUSDT"}, {"AAAUSDT": 1.0}
    )
    assert got_commentary == commentary, "commentary reached the journal whole"
    assert intents[0].reason == reason, "the intent's reason reached the journal whole"


# -- input: the same slice must not survive anywhere ------------------------


def test_no_blind_payload_slice_remains_in_the_model_path():
    """A grep test, deliberately. Both sites carried `[:20000]` and only one was
    found by reading the symptom; the other turned up by searching for the
    shape. If a third appears, it should fail here rather than in production."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "trading"
    offenders = []
    for path in root.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # Prose describing the defect is not the defect. Comments and the
            # docstrings that record what went wrong must stay readable.
            if stripped.startswith("#") or "`" in line:
                continue
            if re.search(r"\)\[:\d{4,}\]", line):
                offenders.append(f"{path.name}:{n}: {stripped}")
    assert not offenders, "blind truncation of a model payload: " + "; ".join(offenders)
