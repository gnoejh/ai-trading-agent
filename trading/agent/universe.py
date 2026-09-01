"""The tradable universe and the screen that narrows it.

The universe is the whole market — roughly 2,650 KR listings and 6,673 US — and
it is fetched from the broker, never hand-maintained, because a stale hardcoded
list silently trades delisted or halted symbols.

That size forces the shape of the pipeline. Quoting 2,650 symbols every cycle is
not feasible and no model can weigh them in one prompt, so the funnel is:

    full universe  ->  broker ranking screens  ->  top N candidates  ->  model

Screening is deterministic and runs before any model call, which also bounds
cost: the model's context is a function of `screen.candidates`, not of market
size. Ranking endpoints already return the liquid, moving names, so the screen
is a union of ranked lists scored by best rank achieved across them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from trading.brokers.kiwoom.client import KiwoomClient
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Listing:
    code: str
    name: str
    market_name: str = ""
    state: str = ""
    audit: str = ""
    last_price: float = 0.0
    # US quotes require the listing's own exchange (NY/ND/NA); `%` is accepted by
    # the LIST endpoint but rejected per-symbol with error 1903.
    exchange: str = ""


def _f(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


def _rows(payload: dict) -> list[dict]:
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


class Universe:
    """Broker-sourced tradable list, cached to disk between refreshes."""

    def __init__(self, client: KiwoomClient, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.client = client
        self.market = str(client.market)
        self.ucfg = self.cfg.agent.universe
        self.cache_path = Path(self.ucfg.cache) / f"{self.market}.json"
        self._listings: list[Listing] = []
        self._index: dict[str, Listing] = {}

    # -- fetch ----------------------------------------------------------------

    def _fetch(self) -> list[Listing]:
        mcfg = self.ucfg.market(self.market)
        segments = mcfg.segments or {}
        # One call per market segment (코스피 / 코스닥 / ...), or a single call
        # when the endpoint takes no segment discriminator.
        combos: list[dict] = [{}]
        for key, values in segments.items():
            combos = [{**c, key: v} for c in combos for v in values]

        out: list[Listing] = []
        for combo in combos:
            body = {**mcfg.list.params, **combo}
            try:
                payload = self.client.call(mcfg.list.api_id, body).body
            except Exception as exc:  # noqa: BLE001 - one segment failing is survivable
                log.error("universe fetch failed for %s: %s", body, exc)
                continue
            for row in _rows(payload):
                market_name = str(row.get(mcfg.market_name_field, ""))
                if (
                    mcfg.exclude_etf
                    and mcfg.etf_field
                    and str(row.get(mcfg.etf_field, "")).upper() == "Y"
                ):
                    continue
                # ETF/ETN/리츠 share this endpoint with common stock; they are
                # different instruments and are not what this agent trades.
                if mcfg.include_market_names and market_name not in mcfg.include_market_names:
                    continue
                audit = str(row.get("auditInfo", ""))
                if mcfg.require_audit_normal and audit != "정상":
                    continue
                state = str(row.get("state", ""))
                if any(bad in state for bad in mcfg.exclude_states):
                    continue
                code = str(row.get("code") or row.get("stk_cd") or "").strip()
                if not code:
                    continue
                out.append(
                    Listing(
                        code=code,
                        name=str(row.get("name") or row.get("stk_nm") or ""),
                        market_name=market_name,
                        state=state,
                        audit=audit,
                        last_price=_f(row.get("lastPrice") or row.get("cur_prc")),
                        exchange=str(row.get(mcfg.exchange_field, ""))
                        if mcfg.exchange_field
                        else "",
                    )
                )
        log.info("universe %s: %d tradable listings", self.market, len(out))
        return out

    # -- cache ----------------------------------------------------------------

    def _cache_age_h(self) -> float:
        if not self.cache_path.exists():
            return float("inf")
        mtime = dt.datetime.fromtimestamp(self.cache_path.stat().st_mtime, dt.UTC)
        return (dt.datetime.now(dt.UTC) - mtime).total_seconds() / 3600

    def _save(self, listings: list[Listing]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps([dataclasses.asdict(x) for x in listings], ensure_ascii=False),
            encoding="utf-8",
        )

    def _load(self) -> list[Listing]:
        raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        return [Listing(**r) for r in raw]

    def listings(self, *, force: bool = False) -> list[Listing]:
        if self._listings and not force:
            return self._listings
        if not force and self._cache_age_h() < self.ucfg.refresh_hours:
            try:
                self._listings = self._load()
                log.info("universe %s: %d from cache", self.market, len(self._listings))
                return self._listings
            except (OSError, ValueError, TypeError) as exc:
                log.warning("universe cache unreadable (%s), refetching", exc)
        self._listings = self._fetch()
        self._index = {}
        if self._listings:
            self._save(self._listings)
        return self._listings

    @property
    def codes(self) -> set[str]:
        return {x.code for x in self.listings()}

    def exchange_of(self, code: str) -> str:
        """The listing's own exchange, needed by per-symbol US endpoints."""
        return self._by_code().get(code, Listing(code="", name="")).exchange

    def _by_code(self) -> dict[str, Listing]:
        if not self._index:
            self._index = {x.code: x for x in self.listings()}
        return self._index

    def name_of(self, code: str) -> str:
        for x in self.listings():
            if x.code == code:
                return x.name
        return ""


class Screen:
    """Reduces the universe to the candidates a model may consider."""

    def __init__(self, client: KiwoomClient, universe: Universe, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.client = client
        self.universe = universe
        self.scfg = self.cfg.agent.screen

    def candidates(self) -> list[dict]:
        """Union of the ranking screens, scored by best rank across them."""
        tradable = self.universe.codes
        scored: dict[str, dict] = {}

        market_screen = self.scfg.market(self.universe.market)
        min_price = market_screen.min_price or self.scfg.min_price
        for ranker in market_screen.rankers:
            try:
                payload = self.client.call(ranker.api_id, dict(ranker.params)).body
            except Exception as exc:  # noqa: BLE001 - a dead screen must not stop the cycle
                log.error("screen %s failed: %s", ranker.api_id, exc)
                continue
            for position, row in enumerate(_rows(payload), start=1):
                code = str(row.get("stk_cd") or row.get("code") or "").strip()
                # A ranked name that is not in the tradable universe (halted,
                # delisted, wrong segment) must never reach the model.
                if not code or code not in tradable:
                    continue
                price = _f(row.get("cur_prc"))
                if min_price and price and abs(price) < min_price:
                    continue
                chg = abs(_f(row.get("flu_rt"))) / 100
                if market_screen.min_change_pct and chg and chg < market_screen.min_change_pct:
                    continue
                if market_screen.max_change_pct and chg > market_screen.max_change_pct:
                    continue
                entry = scored.setdefault(
                    code,
                    {
                        "symbol": code,
                        "name": row.get("stk_nm") or self.universe.name_of(code),
                        "price": abs(price),
                        "change_pct": row.get("flu_rt"),
                        "volume": row.get("trde_qty") or row.get("now_trde_qty"),
                        "best_rank": position,
                        "screens": [],
                    },
                )
                entry["best_rank"] = min(entry["best_rank"], position)
                entry["screens"].append(ranker.api_id)

        ranked = sorted(scored.values(), key=lambda e: (-len(e["screens"]), e["best_rank"]))
        selected = ranked[: self.scfg.candidates]
        log.info(
            "screen %s: %d ranked -> %d candidates",
            self.universe.market,
            len(scored),
            len(selected),
        )
        return selected
