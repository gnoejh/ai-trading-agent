"""The broker adapter seam.

`TradingAgent` needs six things from a broker: reconciled state, screened
candidates, prices, the venue's order rules, an executor, and the fee book a
symbol belongs to. Everything else — the gate, the sizer, the exit supervisor,
the cost ledger — is venue-agnostic already.

Binance is the only venue since 2026-09-01 (the Kiwoom side was removed when the
project became Binance-only), but the seam stays: everything downstream talks to
`BrokerAdapter`, never to the client, so a second venue is an adapter away.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Protocol

from trading.brokers.binance.account import BinanceAccountState, BinanceExecutor
from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.universe import BinanceScreen, BinanceUniverse
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


def _num(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


class BrokerAdapter(Protocol):
    market: str

    def state(self): ...
    def executor(self, gate): ...
    def candidates(self, order_size: float) -> list[dict]: ...
    def prices(self, symbols: list[str]) -> dict[str, float]: ...
    def rules_for(self, symbol: str): ...
    def holdings(self, snapshot) -> dict[str, dict]: ...
    def fee_market(self, symbol: str) -> str: ...
    def realised_pnl(self, symbols=None) -> float | None: ...


class BinanceAdapter:
    """Crypto and bStocks over one shared USDT balance — one agent, two books."""

    def __init__(self, market: str = "BINANCE", cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.market = market
        books = list(self.cfg.broker.binance.markets)
        # Book-agnostic connection: the client is bound to one market only for its
        # quote asset, which both books share.
        self.client = BinanceClient(books[0], cfg=self.cfg)
        self.universe = BinanceUniverse(self.client, books, self.cfg)
        self.screen = BinanceScreen(self.client, self.universe, self.cfg)
        self._state = BinanceAccountState(self.client, self.universe, self.cfg)
        # symbol -> (quantity, basis). See _cost_basis for why quantity is the key.
        self._basis: dict[str, tuple[float, float]] = {}

    def state(self):
        return self._state

    def executor(self, gate):
        return BinanceExecutor(self.client, gate, self.universe, self.cfg)

    def candidates(self, order_size: float = 0.0) -> list[dict]:
        return self.screen.candidates(order_size=order_size)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        return self._state.prices(symbols)

    def rules_for(self, symbol: str):
        return self.universe.rules_for(symbol)

    def _cost_basis(self, symbol: str, qty: float) -> float:
        """myTrades-backed basis, cached per (symbol, quantity).

        The Spot Testnet seeds ~480 assets this system never bought, and
        discovering "no fills" for each cost one signed myTrades call per symbol
        PER CYCLE — ~48s of API traffic to learn nothing new. Quantity is the
        invalidation key: any fill (and any deposit) changes the balance, which
        forces a fresh read, while a balance that has not moved cannot have new
        fills that change its basis.
        """
        cached = self._basis.get(symbol)
        if cached and cached[0] == qty:
            return cached[1]
        basis = self._state.cost_basis(symbol)
        self._basis[symbol] = (qty, basis)
        return basis

    def holdings(self, snapshot) -> dict[str, dict]:
        out = {}
        for row in snapshot.positions.get("rows", []):
            symbol = row.get("stk_cd")
            qty = _num(row.get("rmnd_qty"))
            if symbol and qty > 0:
                cost_basis = self._cost_basis(symbol, qty)
                out[symbol] = {
                    "quantity": qty,
                    # The stop is built from actual fill basis, never the current
                    # mark, otherwise a trailing stop will chase the falling price
                    # and never fire.
                    "avg_price": cost_basis,
                    # The only value an exit plan may use as entry.
                    "cost_basis": cost_basis,
                }
        return out

    def fee_market(self, symbol: str) -> str:
        """A symbol's own book decides its hurdle: crypto 0.5%, bStocks 0.6%."""
        return self.universe.book_of(symbol) or self.market

    def realised_pnl(self, symbols=None) -> float | None:
        """Realised P&L from the broker's own fill records.

        Binance reports fills, not P&L, so it is reconstructed from `myTrades`:
        sells bring quote currency in, buys take it out, commissions come off
        both. That is exact for a symbol whose position is flat, and is the
        correct figure whenever the ladder has fully unwound.

        `myTrades` requires a symbol, so only symbols this system has touched are
        queried -- there is no account-wide endpoint to abuse.
        """
        if not symbols:
            return None
        cutoff = int(
            dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            * 1000
        )
        total = 0.0
        seen = False
        for symbol in symbols:
            try:
                rows = self.client.call(
                    "my_trades", {"symbol": symbol, "startTime": cutoff, "limit": 500}
                ).body.get("rows", [])
            except Exception as exc:  # noqa: BLE001 - one symbol failing is survivable
                log.warning("myTrades failed for %s: %s", symbol, exc)
                continue
            for t in rows:
                seen = True
                quote = _num(t.get("quoteQty"))
                total += quote if not t.get("isBuyer") else -quote
                # Commission in the quote asset is a direct deduction; in another
                # asset it is already reflected in the received quantity.
                if str(t.get("commissionAsset", "")) == self.client.market_cfg.quote_asset:
                    total -= _num(t.get("commission"))
        return total if seen else None


def build_adapter(broker: str, market: str | None = None, cfg: AppConfig | None = None):
    cfg = cfg or config()
    if broker.lower() == "binance":
        # `market` is accepted for signature stability and ignored: Binance is
        # one market spanning both books.
        return BinanceAdapter("BINANCE", cfg)
    raise KeyError(f"unknown broker {broker!r}; known: binance")
