"""Telegram surface tests, especially the inbound allowlist. No network."""

from __future__ import annotations

import httpx
import pytest

from trading.config import TelegramSecrets, load_config
from trading.notify.telegram import TelegramNotifier

ALLOWED = 962508388
STRANGER = 111222333


@pytest.fixture
def secrets():
    # _env_file=None: never let the real bot token into a test or its output.
    return TelegramSecrets(
        _env_file=None, TRADING_AGENT_BOT_TOKEN="test:token", TELEGRAM_ALLOWED_IDS=str(ALLOWED)
    )


def make(secrets, handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    n = TelegramNotifier(
        cfg=load_config(),
        secrets=secrets,
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
    )
    return n, calls


def ok(_request):
    return httpx.Response(200, json={"ok": True, "result": {}})


def updates(*items):
    def handler(_request):
        return httpx.Response(200, json={"ok": True, "result": list(items)})

    return handler


def update(update_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"username": "u"}},
    }


def test_send_defaults_to_allowed_ids(secrets):
    n, calls = make(secrets, ok)
    assert n.send("hello") is True
    assert calls[-1].url.path.endswith("/sendMessage")
    import json

    assert json.loads(calls[-1].content)["chat_id"] == ALLOWED


def test_send_never_raises_on_transport_error(secrets):
    def boom(_request):
        raise httpx.ConnectError("network down")

    n, _ = make(secrets, boom)
    assert n.send("hello") is False  # logged, not raised


def test_send_never_raises_on_api_error(secrets):
    n, _ = make(
        secrets, lambda r: httpx.Response(403, json={"ok": False, "description": "blocked"})
    )
    assert n.send("hello") is False


def test_long_text_is_split(secrets):
    n, calls = make(secrets, ok)
    limit = n.cfg.max_message_chars
    n.send("x" * (limit + 10))
    sends = [c for c in calls if c.url.path.endswith("/sendMessage")]
    assert len(sends) == 2


def test_poll_returns_allowed_messages(secrets):
    n, _ = make(secrets, updates(update(1, ALLOWED, "status")))
    msgs = n.poll(timeout_s=0)
    assert [m.text for m in msgs] == ["status"]


def test_poll_drops_unauthorised_chat(secrets):
    n, _ = make(secrets, updates(update(1, STRANGER, "buy everything")))
    assert n.poll(timeout_s=0) == []


def test_offset_advances_past_dropped_messages(secrets):
    """A stranger's update must not wedge the loop by never being consumed."""
    n, _ = make(secrets, updates(update(7, STRANGER, "hi")))
    n.poll(timeout_s=0)
    assert n._offset == 8


def test_mixed_batch_keeps_only_allowed(secrets):
    n, _ = make(secrets, updates(update(1, STRANGER, "a"), update(2, ALLOWED, "b")))
    msgs = n.poll(timeout_s=0)
    assert [m.chat_id for m in msgs] == [ALLOWED]
    assert n._offset == 3


def test_unconfigured_notifier_is_inert():
    n = TelegramNotifier(
        cfg=load_config(),
        secrets=TelegramSecrets(_env_file=None, TRADING_AGENT_BOT_TOKEN=""),
        client=httpx.Client(transport=httpx.MockTransport(ok)),
    )
    assert n.configured is False
    assert n.send("hello") is False
    assert n.poll(timeout_s=0) == []


def test_broken_markup_is_resent_as_plain_text(secrets):
    """Telegram rejects the whole message on a markup error; an alert must not be lost."""
    calls = []

    def handler(request):
        import json as _j

        payload = _j.loads(request.content)
        calls.append(payload)
        if "parse_mode" in payload:
            return httpx.Response(
                400,
                json={"ok": False, "description": "Bad Request: can't parse entities"},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    n, _ = make(secrets, handler)
    assert n.send("sizing: full_balance, max 1 position(s)") is True
    assert len(calls) == 2
    assert "parse_mode" in calls[0] and "parse_mode" not in calls[1]


def test_a_genuine_failure_is_not_retried_forever(secrets):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})

    n, _ = make(secrets, handler)
    assert n.send("hello") is False
    assert len(calls) == 1, "only markup errors warrant a resend"
