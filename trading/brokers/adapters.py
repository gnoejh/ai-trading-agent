"""One interface, two venues.

`TradingAgent` needs six things from a broker: reconciled state, screened
candidates, prices, the venue's order rules, an executor, and the fee book a
symbol belongs to. Everything else — the gate, the sizer, the exit supervisor,
the cost ledger — is venue-agnostic already.

Kiwoom and Binance are separate accounts and run as separate agents, so an
adapter never has to reconcile two venues against one balance. What it does is
absorb the differences that would otherwise leak into the loop:

* Kiwoom prices come one call per symbol; Binance returns every price in one.
* Kiwoom quantities are whole shares; Binance quantises per symbol.
* Kiwoom has a session; crypto does not.
* A Binance symbol's fee book (crypto vs bStocks) is a property of the symbol,
  not of the agent, because the two hurdles differ (0.5% vs 0.6%).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Protocol
from zoneinfo import ZoneInfo

from trading.agent.universe import Screen, Universe, _rows
from trading.brokers.binance.account import BinanceAccountState, BinanceExecutor
from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.universe import BinanceScreen, BinanceUniverse
from trading.brokers.kiwoom.account import AccountState
from trading.brokers.kiwoom.client import KiwoomClient
from trading.brokers.kiwoom.orders import OrderExecutor
from trading.config import AppConfig, config
from trading.rag.spec_parser import Market

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


class KiwoomAdapter:
    """KR or US equities via Kiwoom."""

    def __init__(self, market: str, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.market = market
        self.client = KiwoomClient(Market(market))
        self._state = AccountState(self.client)
        self.universe = Universe(self.client, self.cfg)
        self.screen = Screen(self.client, self.universe, self.cfg)

    def state(self):
        return self._state

    def normalise(self, snap):
        """Give the US snapshot the canonical field names the gate and sizer read.

        The KR and US endpoints answer with entirely different vocabularies --
        KR: prsm_dpst_aset_amt / entr / rmnd_qty / pur_pric
        US: tot_evlt_amt      / d0_usd_fx_entr / qty / frgn_stk_book_uv
        Reading KR names off a US response yielded equity 0 and cash 0, so the
        gate rejected every buy as "equity unavailable" and the sizer produced
        quantity 0. Silent, and it would have looked like "no opportunities".
        """
        if self.market != "US":
            return snap
        pos, cash = snap.positions, snap.cash
        holdings = _rows(pos)
        equity = _num(pos.get("tot_evlt_amt"))
        # d0_usd_fx_entr is the settled USD deposit; the won fields are irrelevant
        # to a USD-denominated order.
        usd_cash = _num(cash.get("d0_usd_fx_entr"))
        pos.setdefault("prsm_dpst_aset_amt", equity + usd_cash)
        cash.setdefault("ord_alow_amt", usd_cash)
        cash.setdefault("entr", usd_cash)
        for row in holdings:
            # sell_alowq is what may actually be sold today; qty can include
            # unsettled buys that the venue will reject on a sell.
            row.setdefault("rmnd_qty", row.get("sell_alowq") or row.get("poss_qty") or row.get("qty"))
            row.setdefault("pur_pric", row.get("frgn_stk_book_uv"))
            row.setdefault("stk_nm", row.get("frgn_stk_nm"))
        return snap

    def executor(self, gate):
        return OrderExecutor(self.client, gate, self.cfg)

    def candidates(self, order_size: float = 0.0) -> list[dict]:
        return self.screen.candidates()

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """One call per symbol — Kiwoom has no bulk price endpoint."""
        basic = self.cfg.agent.quotes_for(self.market).get("basic")
        if basic is None:
            return {}
        needs_exchange = "stex_tp" in self.client.store.get(basic.api_id).required_body()
        out: dict[str, float] = {}
        for symbol in symbols:
            body = {**basic.params, "stk_cd": symbol}
            if needs_exchange:
                exchange = self.universe.exchange_of(symbol)
                if not exchange:
                    log.warning("no exchange known for %s; quote skipped", symbol)
                    continue
                body["stex_tp"] = exchange
            try:
                # The sign encodes direction, not magnitude: -230500 is 230,500 down.
                price = abs(_num(self.client.call(basic.api_id, body).body.get("cur_prc")))
            except Exception as exc:  # noqa: BLE001 - a missing quote is not fatal
                log.warning("quote failed for %s: %s", symbol, exc)
                continue
            if price:
                out[symbol] = price
        return out

    def rules_for(self, symbol: str):
        return None  # KRX trades whole shares; no per-symbol quantisation

    def holdings(self, snapshot) -> dict[str, dict]:
        self.normalise(snapshot)
        out = {}
        for row in _rows(snapshot.positions):
            code = str(row.get("stk_cd", "")).lstrip("A")
            qty = _num(row.get("rmnd_qty") or row.get("hldg_qty"))
            if code and qty > 0:
                basis = abs(_num(row.get("pur_pric") or row.get("avg_prc")))
                out[code] = {"quantity": qty, "avg_price": basis, "cost_basis": basis}
        return out

    def fee_market(self, symbol: str) -> str:
        return self.market

    def realised_pnl(self, symbols=None) -> float | None:
        """Today's realised P&L, as the BROKER reports it (일자별실현손익).

        Never derived from equity change: the owner deposits and withdraws, and
        an equity difference would book those as trading performance.
        """
        ep = self.cfg.risk.daily_pnl
        if ep is None:
            return None
        today = dt.datetime.now(ZoneInfo(self.cfg.broker.kiwoom.timezone)).strftime("%Y%m%d")
        try:
            body = self.client.call(
                ep.api_id, {**ep.params, "strt_dt": today, "end_dt": today}
            ).body
        except Exception as exc:  # noqa: BLE001 - reporting must not break a cycle
            log.warning("realised P&L unavailable: %s", exc)
            return None
        return _num(body.get("rlzt_pl"))


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

    def holdings(self, snapshot) -> dict[str, dict]:
        out = {}
        for row in snapshot.positions.get("rows", []):
            symbol = row.get("stk_cd")
            qty = _num(row.get("rmnd_qty"))
            if symbol and qty > 0:
                cost_basis = self._state.cost_basis(symbol)
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
    match broker.lower():
        case "kiwoom":
            return KiwoomAdapter(market or cfg.agent.market, cfg)
        case "binance":
            # `market` names a Kiwoom surface (KR/US) and is meaningless here:
            # Binance is one market spanning both books, so it is ignored rather
            # than passed through, which would otherwise label the agent "KR" and
            # gate it on the Seoul session clock.
            return BinanceAdapter("BINANCE", cfg)
    raise KeyError(f"unknown broker {broker!r}; known: kiwoom, binance")
