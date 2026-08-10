"""Verify every configured LLM tier can actually reach its provider.

    uv run python -m trading.llm.check

Sends a one-token prompt per tier and reports the round trip. Cheap enough to run
whenever a key or base URL changes.
"""

from __future__ import annotations

import time

from trading.config import LLMSecrets, config
from trading.llm.client import LLMClient


def main() -> int:
    cfg = config().llm
    secrets = LLMSecrets()
    llm = LLMClient()
    failures = 0

    for name, tier in cfg.tiers.items():
        key = secrets.key_for(tier.provider)
        base_url = cfg.provider(tier.provider).base_url
        label = f"{name:<10} {tier.provider}/{tier.model}"
        if not key:
            print(f"[ SKIP ] {label}\n         no API key in .env for {tier.provider}")
            failures += 1
            continue
        started = time.monotonic()
        try:
            reply = llm.ask("Reply with the single word: ok", tier=name)
            print(f"[  OK  ] {label}  {time.monotonic() - started:.2f}s  -> {reply.strip()[:40]!r}")
        except Exception as exc:  # noqa: BLE001 - a report, not a control path
            print(f"[ FAIL ] {label}\n         {base_url}\n         {type(exc).__name__}: {exc}")
            failures += 1

    print(f"\n{len(cfg.tiers) - failures}/{len(cfg.tiers)} tiers reachable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
