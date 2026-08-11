"""The Binance universe and screen — crypto and bStocks over one shared balance.

Both books settle in the same USDT wallet, so they are treated as **one universe
with a book tag**, not two agents. Two agents would race: each would read the
full balance, size against it, and the second order would be rejected for
insufficient funds — or worse, both fill and the account is twice as committed as
intended. One agent choosing across the union cannot race itself.

The tag still matters after selection, because the two books are not economically
alike: a bStocks round trip costs 0.6% against crypto's 0.5%, so fees and exit
targets are resolved per symbol's book rather than per agent.

Screening uses `ticker/24hr` — one call returns every symbol's rolling volume and
change, so the funnel needs no per-symbol requests at all. That is a meaningful
difference from Kiwoom, where the screen costs one call per ranking endpoint and
quotes cost one call per candidate.
"""

from __future__ import annotations

import logging

from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.symbols import SymbolBook, SymbolRules
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class BinanceUniverse:
    """Union of the configured books, each symbol tagged with the book it belongs to."""

    def __init__(self, client: BinanceClient, books: list[str], cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.client = client
        self.books = books
        self._rules: dict[str, SymbolRules] = {}
        self._book_of: dict[str, str] = {}

    def load(self, *, force: bool = False) -> dict[str, SymbolRules]:
        if self._rules and not force:
            return self._rules
        rules: dict[str, SymbolRules] = {}
        book_of: dict[str, str] = {}
        for book in self.books:
            market_cfg = self.cfg.broker.binance.market(book)
            for symbol, rule in SymbolBook(self.client, market_cfg).load(force=force).items():
                rules[symbol] = rule
                book_of[symbol] = book
        self._rules, self._book_of = rules, book_of
        log.info(
            "binance universe: %d symbols across %s",
            len(rules),
            ", ".join(f"{b}={sum(1 for v in book_of.values() if v == b)}" for b in self.books),
        )
        return rules

    def book_of(self, symbol: str) -> str:
        """Which book a symbol belongs to — decides its fee model and hurdle."""
        self.load()
        return self._book_of.get(symbol, "")

    def rules_for(self, symbol: str) -> SymbolRules | None:
        return self.load().get(symbol)

    @property
    def symbols(self) -> set[str]:
        return set(self.load())


class BinanceScreen:
    """Narrows the union to the candidates a model may consider.

    One `ticker/24hr` call covers every symbol, so both the liquidity and the
    momentum screen come from a single request.
    """

    def __init__(
        self, client: BinanceClient, universe: BinanceUniverse, cfg: AppConfig | None = None
    ):
        self.cfg = cfg or config()
        self.client = client
        self.universe = universe
        self.scfg = self.cfg.agent.screen

    def _flow(self, symbols: list[str]) -> dict[str, float]:
        """Average taker-buy share over the last N bars, per symbol.

        `takerBuyBaseVolume` (kline field 9) is the portion of volume from buyers
        crossing the spread -- aggression, not just price. Measured over 1,000
        daily bars on 10 majors, the top decile of 5-day average taker-buy share
        returned +1.58% over the next 5 days against +0.53% for the bottom decile:
        a 1.05% spread, roughly twice the round-trip cost. Over the same data the
        3-day price change spread was -0.06% -- indistinguishable from noise.

        So flow ranks the shortlist and price momentum does not.
        """
        cfg = self.scfg
        out: dict[str, float] = {}
        for symbol in symbols:
            try:
                rows = self.client.call(
                    "klines",
                    {"symbol": symbol, "interval": cfg.flow_interval, "limit": cfg.flow_lookback},
                ).body.get("rows", [])
            except Exception as exc:  # noqa: BLE001 - a missing flow reading is not fatal
                log.warning("flow unavailable for %s: %s", symbol, exc)
                continue
            shares = [float(k[9]) / float(k[5]) for k in rows if len(k) > 9 and float(k[5]) > 0]
            if shares:
                out[symbol] = sum(shares) / len(shares)
        return out

    def candidates(self, order_size: float = 0.0) -> list[dict]:
        tradable = self.universe.symbols
        rows = [
            r
            for r in self.client.call("ticker_24hr").body.get("rows", [])
            if r.get("symbol") in tradable
        ]
        if not rows:
            log.error("binance screen: ticker/24hr returned nothing tradable")
            return []

        screen_cfg = self.scfg.BINANCE
        min_price = (screen_cfg.min_price if screen_cfg else 0.0) or 0.0
        excluded = {a.upper() for a in self.scfg.exclude_assets}
        # The liquidity bar is the greater of the absolute floor and a multiple of
        # the order, so it rises with the account instead of going stale.
        scaled_floor = order_size * self.scfg.min_volume_multiple_of_order

        by_book: dict[str, list[dict]] = {}
        for r in rows:
            symbol = r["symbol"]
            rules = self.universe.rules_for(symbol)
            if rules and rules.base_asset.upper() in excluded:
                continue
            price, volume = _f(r.get("lastPrice")), _f(r.get("quoteVolume"))
            if min_price and price < min_price:
                continue
            chg = abs(_f(r.get("priceChangePercent"))) / 100
            if self.scfg.min_change_pct and chg < self.scfg.min_change_pct:
                continue
            if self.scfg.max_change_pct and chg > self.scfg.max_change_pct:
                continue
            book = self.universe.book_of(symbol)
            floor = max(
                self.scfg.book_min_quote_volume.get(book, self.scfg.min_quote_volume),
                scaled_floor,
            )
            if floor and volume < floor:
                continue
            by_book.setdefault(book, []).append(
                {
                    "symbol": symbol,
                    "book": book,
                    "price": price,
                    "change_pct": _f(r.get("priceChangePercent")),
                    "quote_volume": volume,
                }
            )

        # Flow is measured only for the liquidity-qualified pool: one kline call
        # per symbol is affordable for ~60 names, not for 489.
        flow = {}
        if self.scfg.use_flow:
            shortlist = [
                e["symbol"]
                for pool in by_book.values()
                for e in sorted(pool, key=lambda x: -x["quote_volume"])[
                    : self.scfg.flow_pool_per_book
                ]
            ]
            flow = self._flow(shortlist)

        selected: list[dict] = []
        pool_size = 0
        for book, pool in by_book.items():
            pool_size += len(pool)
            for e in pool:
                e["taker_buy_share"] = flow.get(e["symbol"])
            if self.scfg.use_flow:
                pool = [e for e in pool if e.get("taker_buy_share") is not None]
            slots = self.scfg.book_slots.get(book, self.scfg.candidates)
            by_volume = sorted(pool, key=lambda e: -e["quote_volume"])
            # Rank by BUY PRESSURE, not by price change -- the measured signal.
            by_move = (
                sorted(pool, key=lambda e: -(e.get("taker_buy_share") or 0))
                if self.scfg.use_flow
                else sorted(pool, key=lambda e: -abs(e["change_pct"]))
            )

            # Union of ranked lists, same shape as the Kiwoom screen: appearing in
            # both is the strongest signal. Ranked WITHIN the book, so a thin venue
            # is judged against its own liquidity rather than against crypto's.
            scored: dict[str, dict] = {}
            for ranked, label in ((by_volume, "volume"), (by_move, "move")):
                for position, entry in enumerate(ranked[: slots * 4], start=1):
                    got = scored.setdefault(
                        entry["symbol"], {**entry, "best_rank": position, "screens": []}
                    )
                    got["best_rank"] = min(got["best_rank"], position)
                    got["screens"].append(label)
            ranked = sorted(scored.values(), key=lambda e: (-len(e["screens"]), e["best_rank"]))
            selected.extend(ranked[:slots])

        log.info(
            "binance screen: %d in pool -> %d candidates (%s)",
            pool_size,
            len(selected),
            ", ".join(f"{b}={sum(1 for x in selected if x['book'] == b)}" for b in by_book),
        )
        return selected
