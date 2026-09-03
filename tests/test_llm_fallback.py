"""An empty reasoner reply must retry without thinking, never become a decision.

Observed 2026-09-03: three decide calls returned empty content after ~120k
chars of reasoning (finish=length). The configured fallback was the decide
tier itself, which `ask` rightly refuses to retry -- so there was no fallback.
No network: the OpenAI client is replaced by a recorder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading.config import load_config
from trading.llm.client import LLMClient, LLMNoAnswer


def _reply(content, reasoning="", finish="stop"):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=None
    )


class _Recorder:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


@pytest.fixture
def cfg():
    c = load_config()
    c.llm.default_tier = "fast"
    c.llm.fallback_tier = "fast_nothink"
    return c


def _client(cfg, recorder):
    llm = LLMClient(cfg, ledger=False)
    llm._client_for = lambda provider: recorder
    return llm


def test_config_fallback_is_a_distinct_no_thinking_tier():
    llm = load_config().llm
    assert llm.fallback_tier != llm.default_tier
    assert llm.tier(llm.fallback_tier).thinking == "disabled"
    assert llm.tier(llm.default_tier).thinking is None


def test_empty_reasoning_reply_retries_with_thinking_disabled(cfg):
    rec = _Recorder([_reply("", reasoning="x" * 120000, finish="length"), _reply('{"ok": 1}')])
    out = _client(cfg, rec).ask("decide", tier="fast")
    assert out == '{"ok": 1}'
    assert len(rec.calls) == 2
    assert "extra_body" not in rec.calls[0]
    assert rec.calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert rec.calls[1]["model"] == cfg.llm.tier("fast").model


def test_good_reply_never_falls_back(cfg):
    rec = _Recorder([_reply("fine")])
    assert _client(cfg, rec).ask("decide", tier="fast") == "fine"
    assert len(rec.calls) == 1


def test_both_silent_raises_not_empty_string(cfg):
    rec = _Recorder([_reply("", reasoning="r", finish="length"), _reply("", finish="length")])
    with pytest.raises(LLMNoAnswer, match="both silent"):
        _client(cfg, rec).ask("decide", tier="fast")
