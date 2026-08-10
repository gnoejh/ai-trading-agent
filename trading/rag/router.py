"""Route a natural-language question to Kiwoom API ids.

Stage 1 of the retrieval design in :mod:`trading.rag.spec_store`: the model reads
the market-scoped catalog and names the API ids it needs. Stage 2 is a plain
dict lookup, so nothing depends on embedding recall.

The catalog is always scoped to one market -- an unscoped list invites the model
to answer a KR question with a US endpoint.
"""

from __future__ import annotations

import json
import logging
import re

from trading.llm.client import LLMClient
from trading.rag.spec_parser import Market
from trading.rag.spec_store import SpecStore

log = logging.getLogger(__name__)

_SYSTEM = """You map questions about the Kiwoom {market} trading API to API ids.

Below is the complete catalog for the {market} market, one API per line:
    <api_id>\t<name>\t<category>

Reply with JSON only: {{"api_ids": ["..."], "reason": "..."}}
Choose the fewest ids that answer the question. If nothing fits, return an empty
list. Never invent an id that is not in the catalog."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class SpecRouter:
    def __init__(self, store: SpecStore | None = None, llm: LLMClient | None = None):
        self.store = store or SpecStore.load()
        self.llm = llm or LLMClient()

    def route(self, question: str, market: Market, *, tier: str | None = None) -> list[str]:
        """Return catalog-verified API ids for `question`."""
        system = _SYSTEM.format(market=market) + "\n\n" + self.store.catalog_prompt(market)
        raw = self.llm.ask(question, system=system, tier=tier)

        m = _JSON.search(raw)
        if not m:
            log.warning("router: no JSON in response: %.200s", raw)
            return []
        ids = json.loads(m.group(0)).get("api_ids", [])

        # Trust nothing: drop ids absent from the catalog or in the wrong market.
        valid = {e.api_id for e in self.store.entries(market)}
        kept = [i for i in ids if i in valid]
        if dropped := [i for i in ids if i not in valid]:
            log.warning("router: dropped invalid ids %s", dropped)
        return kept

    def context(self, question: str, market: Market, *, tier: str | None = None) -> str:
        """Route, then render the full specs -- the prompt for building a call."""
        ids = self.route(question, market, tier=tier)
        return "\n\n".join(self.store.spec_prompt(i) for i in ids)
