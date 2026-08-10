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
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self.complete(messages, tier=tier).content or ""
