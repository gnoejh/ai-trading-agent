"""Telegram surface for the agent — @hjeong_trading_agent_bot.

This is the human-in-the-loop channel: the agent reports here, and the operator
answers here. Two rules make it safe to leave running.

**Inbound chat is untrusted input.** Anyone who finds the bot can message it, so
:meth:`TelegramNotifier.poll` drops every update whose chat id is not in
``TELEGRAM_ALLOWED_IDS`` — dropped, not answered, so the bot does not confirm it
exists. Even for an allowed sender, message text must never reach an order path
without passing the risk gate; a chat message is a request, not an instruction.

**Outbound is best-effort.** A notification failure must never break a trading
cycle, so :meth:`send` logs and returns False rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from trading.config import AppConfig, TelegramSecrets, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Message:
    chat_id: int
    text: str
    update_id: int
    username: str = ""


class TelegramNotifier:
    def __init__(
        self,
        cfg: AppConfig | None = None,
        secrets: TelegramSecrets | None = None,
        client: httpx.Client | None = None,
    ):
        self.cfg = (cfg or config()).notify.telegram
        self.secrets = secrets or TelegramSecrets()
        self.allowed = self.secrets.allowed_ids
        self._http = client or httpx.Client(timeout=self.cfg.timeout_s)
        self._offset: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.secrets.bot_token) and self.cfg.enabled

    def _url(self, method: str) -> str:
        return f"{self.cfg.api_base}/bot{self.secrets.bot_token}/{method}"

    # -- outbound -------------------------------------------------------------

    def _chunks(self, text: str) -> list[str]:
        limit = self.cfg.max_message_chars
        return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]

    def send(self, text: str, chat_id: int | None = None) -> bool:
        """Send to one chat, or to every allowed id. Never raises."""
        if not self.configured:
            log.debug("telegram disabled or unconfigured; dropping message")
            return False
        targets = [chat_id] if chat_id is not None else sorted(self.allowed)
        if not targets:
            log.warning("telegram: no recipients (TELEGRAM_ALLOWED_IDS empty)")
            return False

        ok = True
        for target in targets:
            for chunk in self._chunks(text):
                if not self._send_chunk(target, chunk):
                    ok = False
        return ok

    def _post(self, chat_id: int, text: str, parse_mode: str | None) -> httpx.Response:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._http.post(self._url("sendMessage"), json=payload)

    def _send_chunk(self, target: int, chunk: str) -> bool:
        try:
            r = self._post(target, chunk, self.cfg.parse_mode)
            if r.status_code == 200 and r.json().get("ok"):
                return True

            # Telegram rejects the WHOLE message when markup does not parse, and
            # trading text is full of the characters that break it -- underscores
            # in `full_balance`, asterisks and brackets in reasons. Losing an alert
            # to a formatting error is unacceptable, so resend it as plain text.
            body = r.text[:200]
            if "parse" in body or "entities" in body:
                log.warning("telegram markup rejected; resending unformatted: %s", body)
                r = self._post(target, chunk, None)
                if r.status_code == 200 and r.json().get("ok"):
                    return True
            log.warning("telegram send failed for %s: %s", target, r.text[:200])
            return False
        except httpx.HTTPError as exc:
            # A notification outage must not abort a trading cycle.
            log.warning("telegram send error for %s: %s", target, exc)
            return False

    # -- inbound --------------------------------------------------------------

    def poll(self, timeout_s: int | None = None) -> list[Message]:
        """Long-poll for updates, returning only messages from allowed chats."""
        if not self.configured:
            return []
        params = {"timeout": timeout_s if timeout_s is not None else self.cfg.poll_timeout_s}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            r = self._http.get(
                self._url("getUpdates"),
                params=params,
                timeout=params["timeout"] + self.cfg.timeout_s,
            )
            payload = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram poll error: %s", exc)
            return []
        if not payload.get("ok"):
            log.warning("telegram poll rejected: %s", str(payload)[:200])
            return []

        out: list[Message] = []
        for update in payload.get("result", []):
            # Advance the offset for every update, including rejected ones, so a
            # stranger's message cannot wedge the poll loop by never being consumed.
            self._offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            if chat_id not in self.allowed:
                log.warning("telegram: dropped message from unauthorised chat %s", chat_id)
                continue
            out.append(
                Message(
                    chat_id=chat_id,
                    text=msg["text"],
                    update_id=update["update_id"],
                    username=msg.get("from", {}).get("username", ""),
                )
            )
        return out

    def whoami(self) -> dict:
        """`getMe`, for connectivity checks."""
        r = self._http.get(self._url("getMe"))
        return r.json()

    def close(self) -> None:
        self._http.close()
