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
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.agent.fit import ScorerModel
from trading.brokers.kiwoom.client import KiwoomClient
from trading.config import AppConfig, Ranker, config

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
    """Reduces the universe to the candidates a model may consider.

    Ranking follows the MEASURED signal, not price momentum. The archive
    backfill found the KR edge in 외국인/기관 net buying (+2.01% vs +0.64%
    over three trading days, n=334 a tertile) and no edge in momentum, yet
    the first paper day's menus were all +18..24% intraday movers — which
    the model correctly refused, 23 times out of 23. So the KR screen now
    reads the foreigner/institution net-buy rankings (ka90009, four ranked
    lists in one response), measures every candidate's own net-buy share
    over the last week (ka10061), caps the 24h change, and orders by flow.
    """

    def __init__(self, client: KiwoomClient, universe: Universe, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.client = client
        self.universe = universe
        self.scfg = self.cfg.agent.screen
        self.market = str(universe.market)
        self.market_screen = self.scfg.market(self.market)
        self._quotes: dict[str, tuple[float, dict]] = {}
        self._prior: ScorerModel | None = None
        self._prior_loaded = False

    # -- quotes (cached) ------------------------------------------------------

    def quote(self, symbol: str) -> dict | None:
        """Price, change and volume for one symbol, cached for `quote_cache_s`.

        One call per symbol on Kiwoom; the screen and the observe step ask for
        the same names seconds apart, so the answer is reused.
        """
        ttl = self.cfg.agent.quote_cache_s
        cached = self._quotes.get(symbol)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        basic = self.cfg.agent.quotes_for(self.market).get("basic")
        if basic is None:
            return None
        body = {**basic.params, "stk_cd": symbol}
        if "stex_tp" in self.client.store.get(basic.api_id).required_body():
            exchange = self.universe.exchange_of(symbol)
            if not exchange:
                log.warning("no exchange known for %s; quote skipped", symbol)
                return None
            body["stex_tp"] = exchange
        try:
            raw = self.client.call(basic.api_id, body).body
        except Exception as exc:  # noqa: BLE001 - a missing quote is not fatal
            log.warning("quote failed for %s: %s", symbol, exc)
            return None
        # The sign encodes direction, not magnitude: -230500 is 230,500 down.
        out = {
            "price": abs(_f(raw.get("cur_prc"))),
            "change_pct": raw.get("flu_rt"),
            "volume": raw.get("trde_qty"),
        }
        if out["price"] <= 0:
            return None
        self._quotes[symbol] = (time.monotonic(), out)
        return out

    # -- rankings -------------------------------------------------------------

    def _rankings(self, ranker: Ranker) -> list[tuple[str, list[dict]]]:
        """One ranker -> its ranked list(s), each row normalised.

        A conventional ranker is one list (stk_cd / cur_prc / flu_rt per row).
        A `columns` ranker (외국인기관매매상위) packs several rankings side by
        side — every row holds one name per column — so each column becomes
        its own ranked list, labelled `api_id:column`.
        """
        try:
            payload = self.client.call(ranker.api_id, dict(ranker.params)).body
        except Exception as exc:  # noqa: BLE001 - a dead screen must not stop the cycle
            log.error("screen %s failed: %s", ranker.api_id, exc)
            return []
        rows = _rows(payload)
        if not ranker.columns:
            return [
                (
                    ranker.api_id,
                    [
                        {
                            "code": str(r.get("stk_cd") or r.get("code") or "").strip(),
                            "name": r.get("stk_nm"),
                            "price": _f(r.get("cur_prc")),
                            "change_pct": r.get("flu_rt"),
                            "volume": r.get("trde_qty") or r.get("now_trde_qty"),
                            "amount": _f(r.get("trde_prica")),
                        }
                        for r in rows
                    ],
                )
            ]
        out = []
        for col in ranker.columns:
            ranked = []
            for r in rows:
                code = str(r.get(col.code) or "").strip()
                if not code:
                    continue
                ranked.append(
                    {
                        "code": code,
                        "name": r.get(col.name) if col.name else None,
                        "price": 0.0,
                        "change_pct": None,
                        "volume": None,
                        "amount": _f(r.get(col.amount)) if col.amount else 0.0,
                        "net_buy": True,
                    }
                )
            out.append((f"{ranker.api_id}:{col.code}", ranked))
        return out

    def _flow_share(self, symbol: str) -> float | None:
        """Unit-free investor net-buy share over the configured lookback.

        (foreigner + institution) / (|individual| + |foreigner| + |institution|)
        — the same construction the archive backfill measured, so the live
        feature and the historical buckets speak the same unit.
        """
        flow = self.market_screen.flow
        if flow is None:
            return None
        tz = ZoneInfo(self.cfg.broker.kiwoom.timezone)
        today = dt.datetime.now(tz).date()
        start = today - dt.timedelta(days=flow.lookback_days)
        body = {
            **flow.params,
            "stk_cd": symbol,
            flow.start_field: start.strftime("%Y%m%d"),
            flow.end_field: today.strftime("%Y%m%d"),
        }
        try:
            payload = self.client.call(flow.api_id, body).body
        except Exception as exc:  # noqa: BLE001 - a missing feature is not fatal
            log.warning("flow unavailable for %s: %s", symbol, exc)
            return None
        rows = _rows(payload) or ([payload] if isinstance(payload, dict) else [])
        f = flow.fields
        net = total = 0.0
        for r in rows:
            ind = _f(r.get(f.get("individual", "ind_invsr")))
            frg = _f(r.get(f.get("foreigner", "frgnr_invsr")))
            org = _f(r.get(f.get("institution", "orgn")))
            net += frg + org
            total += abs(ind) + abs(frg) + abs(org)
        return round(net / total, 4) if total > 0 else None

    def _prior_model(self) -> ScorerModel | None:
        if not self._prior_loaded:
            self._prior_loaded = True
            if self.cfg.fit.enabled:
                self._prior = ScorerModel.load(self.cfg.fit.model)
        return self._prior

    def _within_change_bounds(self, change_pct) -> bool:
        if change_pct in (None, ""):
            return True
        chg = abs(_f(change_pct)) / 100
        ms = self.market_screen
        if ms.min_change_pct and chg and chg < ms.min_change_pct:
            return False
        return not (ms.max_change_pct and chg > ms.max_change_pct)

    def candidates(self) -> list[dict]:
        """Union of the ranking screens, then ordered by the venue's signal."""
        tradable = self.universe.codes
        ms = self.market_screen
        min_price = ms.min_price or self.scfg.min_price
        scored: dict[str, dict] = {}

        for ranker in ms.rankers:
            for label, rows in self._rankings(ranker):
                for position, row in enumerate(rows, start=1):
                    code = row["code"]
                    # A ranked name that is not in the tradable universe (halted,
                    # delisted, wrong segment) must never reach the model.
                    if not code or code not in tradable:
                        continue
                    price = row["price"]
                    if min_price and price and abs(price) < min_price:
                        continue
                    if not self._within_change_bounds(row["change_pct"]):
                        continue
                    entry = scored.setdefault(
                        code,
                        {
                            "symbol": code,
                            "name": row.get("name") or self.universe.name_of(code),
                            "price": 0.0,
                            "change_pct": None,
                            "volume": None,
                            "best_rank": position,
                            "screens": [],
                        },
                    )
                    if price and not entry["price"]:
                        entry["price"] = abs(price)
                        entry["change_pct"] = row["change_pct"]
                        entry["volume"] = row["volume"]
                    if row.get("net_buy") and row.get("amount"):
                        entry.setdefault("net_buy_amount", 0.0)
                        entry["net_buy_amount"] += row["amount"]
                    entry["best_rank"] = min(entry["best_rank"], position)
                    entry["screens"].append(label)

        # Names known only from a flow ranking carry no price or change yet:
        # quote them, then apply the same bounds and floors as everyone else.
        for code, entry in list(scored.items()):
            if entry["price"]:
                continue
            q = self.quote(code)
            if q is None:
                del scored[code]
                continue
            entry.update(q)
            if (min_price and entry["price"] < min_price) or not self._within_change_bounds(
                entry["change_pct"]
            ):
                del scored[code]

        ranked = sorted(scored.values(), key=lambda e: (-len(e["screens"]), e["best_rank"]))
        rank_by = ms.rank_by or self.scfg.rank_by
        shortlist = ranked[: self.scfg.candidates * 2] if rank_by != "rank" else ranked
        if ms.flow is not None:
            for e in shortlist:
                e["taker_buy_share"] = self._flow_share(e["symbol"])
        for e in shortlist:
            e["book"] = self.market
            e["quote_volume"] = e["price"] * _f(e.get("volume"))
        shortlist = self._order(shortlist, rank_by)
        selected = shortlist[: self.scfg.candidates]
        log.info(
            "screen %s: %d ranked -> %d candidates (by %s)",
            self.market,
            len(scored),
            len(selected),
            rank_by,
        )
        return selected

    def _order(self, rows: list[dict], rank_by: str) -> list[dict]:
        if rank_by == "flow":
            return sorted(
                rows,
                key=lambda e: (
                    e.get("taker_buy_share") is None,
                    -(e.get("taker_buy_share") or 0.0),
                    e["best_rank"],
                ),
            )
        if rank_by == "model":
            prior = self._prior_model()
            if prior is not None:
                return sorted(rows, key=lambda e: -prior.p_clear(e))
            return self._order(rows, "flow")
        if rank_by == "change":
            return sorted(rows, key=lambda e: -abs(_f(e.get("change_pct"))))
        return rows

    def tradable_pool(self, order_size: float = 0.0) -> list[dict]:
        """Liquid names with the strategy bounds REMOVED — the random arm's pool.

        Built from `pool_rankers` (거래대금상위 by default): the venue already
        ranks by turnover, so liquidity is enforced by construction, and no
        change or flow bound applies — observations gated by the strategy's
        own filters could never falsify the strategy.
        """
        rankers = self.market_screen.pool_rankers or self.market_screen.rankers[:1]
        tradable = self.universe.codes
        min_price = self.market_screen.min_price or self.scfg.min_price
        pool: dict[str, dict] = {}
        for ranker in rankers:
            for _label, rows in self._rankings(ranker):
                for row in rows:
                    code, price = row["code"], abs(row["price"])
                    if not code or code not in tradable or price <= 0 or code in pool:
                        continue
                    if min_price and price < min_price:
                        continue
                    pool[code] = {
                        "symbol": code,
                        "name": row.get("name") or self.universe.name_of(code),
                        "book": self.market,
                        "price": price,
                        "change_pct": _f(row["change_pct"]),
                        "quote_volume": price * _f(row["volume"]),
                    }
        return list(pool.values())
