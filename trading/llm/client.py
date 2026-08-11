"""DeepSeek and Qwen, called directly.

Both expose OpenAI-compatible chat endpoints, so one client covers them and the
difference is a base URL plus a model name -- all of it in `config.yaml` under
`llm.tiers`. Tier names (`fast`, `deep`, `escalation`) are what call sites use;
swapping a provider is a config edit, not a code change.
"""

from __future__ import annotations

import logging

from openai import OpenAI

from trading.accounting.costs import CostLedger, Usage
from trading.config import AppConfig, LLMSecrets, TierConfig, config

log = logging.getLogger(__name__)


class LLMNoAnswer(RuntimeError):
    """The model returned no content. NOT a decision -- an absent answer."""


class LLMClient:
    def __init__(
        self,
        cfg: AppConfig | None = None,
        secrets: LLMSecrets | None = None,
        ledger: CostLedger | None = None,
    ):
        app = cfg or config()
        self.cfg = app.llm
        self.secrets = secrets or LLMSecrets()
        # Pass ledger=False to disable billing (tests, connectivity checks).
        self.ledger = CostLedger(app) if ledger is None else (ledger or None)
        self._clients: dict[str, OpenAI] = {}

    def _client_for(self, provider: str) -> OpenAI:
        if provider not in self._clients:
            api_key = self.secrets.key_for(provider)
            if not api_key:
                raise RuntimeError(f"no API key for provider {provider!r} in .env")
            self._clients[provider] = OpenAI(
                api_key=api_key,
                base_url=self.cfg.provider(provider).base_url,
                timeout=self.cfg.timeout_s,
                max_retries=self.cfg.max_retries,
            )
        return self._clients[provider]

    def complete(
        self,
        messages: list[dict],
        *,
        tier: str | None = None,
        tools: list[dict] | None = None,
        **overrides,
    ):
        """One chat completion at the named tier. Returns the raw message object."""
        t: TierConfig = self.cfg.tier(tier)
        kwargs = {
            "model": t.model,
            "messages": messages,
            "temperature": t.temperature,
            "max_tokens": t.max_tokens,
            **overrides,
        }
        if tools:
            kwargs["tools"] = tools
        resp = self._client_for(t.provider).chat.completions.create(**kwargs)
        self._record(resp, t, tier or self.cfg.default_tier)
        return resp.choices[0].message

    @staticmethod
    def _truncated(message, choice) -> str | None:
        """Detect an answer that never arrived, as distinct from a refusal.

        A reasoning model can spend its whole budget thinking and return EMPTY
        content: observed 29,751 reasoning tokens against an 8,192 cap. The caller
        then sees "" and reads it as "the model declined", which in a trading loop
        is journalled as a considered no-trade. It is not -- it is silence.

        Returns a reason string when the reply is empty for a diagnosable cause.
        """
        if (message.content or "").strip():
            return None
        reasoning = getattr(message, "reasoning_content", None) or ""
        finish = getattr(choice, "finish_reason", None)
        if reasoning:
            return f"empty content after {len(reasoning)} chars of reasoning (finish={finish})"
        return f"empty content (finish={finish})"

    def _record(self, resp, t: TierConfig, tier: str) -> None:
        """Bill every call to the ledger. Token spend is a trading cost."""
        usage = getattr(resp, "usage", None)
        if usage is None or self.ledger is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        try:
            self.ledger.record_llm(
                Usage(
                    model=t.model,
                    provider=t.provider,
                    tier=tier,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    usd=self.ledger.price_call(t.model, prompt_tokens, completion_tokens),
                )
            )
        except Exception:
            log.exception("failed to record llm usage")

    def ask(self, prompt: str, *, system: str | None = None, tier: str | None = None) -> str:
        """Ask, and fall back to another tier if the reply never arrives.

        Silence must never be returned as an empty answer: a caller cannot tell it
        apart from a decision, and downstream it becomes a phantom "no trade".
        """
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        resp = self._client_for(self.cfg.tier(tier).provider)  # noqa: F841 - warms the client
        primary = self.cfg.tier(tier)
        raw = self._client_for(primary.provider).chat.completions.create(
            model=primary.model,
            messages=messages,
            temperature=primary.temperature,
            max_tokens=primary.max_tokens,
        )
        self._record(raw, primary, tier or self.cfg.default_tier)
        choice = raw.choices[0]
        problem = self._truncated(choice.message, choice)
        if problem is None:
            return choice.message.content or ""

        fallback = self.cfg.fallback_tier
        log.error("tier %s returned no answer: %s", tier or self.cfg.default_tier, problem)
        if not fallback or fallback == (tier or self.cfg.default_tier):
            raise LLMNoAnswer(f"{tier}: {problem}")
        log.warning("retrying on %s", fallback)
        fb = self.cfg.tier(fallback)
        raw2 = self._client_for(fb.provider).chat.completions.create(
            model=fb.model,
            messages=messages,
            temperature=fb.temperature,
            max_tokens=fb.max_tokens,
        )
        self._record(raw2, fb, fallback)
        c2 = raw2.choices[0]
        if (p2 := self._truncated(c2.message, c2)) is not None:
            raise LLMNoAnswer(f"{tier} and fallback {fallback} both silent: {problem} / {p2}")
        return c2.message.content or ""
