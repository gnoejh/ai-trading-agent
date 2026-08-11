"""Binance account state and order execution.

Broker records remain the single source of truth. Binance reports holdings as
*asset balances*, not positions, so a "position" here is a non-zero base-asset
balance mapped back to its trading symbol.

Two Binance-specific facts shape this module:

* **`free` vs `locked`.** A resting sell order reserves the asset, so a holding
  under a stop shows `free: 0`. Quantity for exit purposes is free+locked, but
  only `free` can actually be sold without cancelling the resting order first —
  conflating them produces orders the exchange rejects.
* **No settlement delay.** Unlike KR's T+2, proceeds are spendable immediately,
  so orderable cash is simply the free quote balance.
"""

from __future__ import annotations

import datetime as dt
import logging

from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.symbols import SymbolRules
from trading.brokers.kiwoom.account import Snapshot, StaleStateError
from trading.config import AppConfig, config
from trading.risk.gate import Side, Verdict

log = logging.getLogger(__name__)


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class BinanceAccountState:
    """Broker-backed account state. No setters, same contract as the Kiwoom one."""

    def __init__(self, client: BinanceClient, universe, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.client = client
        self.universe = universe
        self.state_cfg = self.cfg.state
        self.quote = client.market_cfg.quote_asset
        self._snapshot: Snapshot | None = None

    # -- reads ----------------------------------------------------------------

    def _balances(self) -> tuple[dict, list[dict]]:
        body = self.client.call("account").body
        rows = body.get("balances", [])
        return body, rows

    def positions_and_cash(self) -> tuple[dict, dict]:
        _, rows = self._balances()
        quote_free = quote_locked = 0.0
        holdings = []
        for b in rows:
            asset = b.get("asset", "")
            free, locked = _f(b.get("free")), _f(b.get("locked"))
            if asset == self.quote:
                quote_free, quote_locked = free, locked
                continue
            total = free + locked
            if total <= 0:
                continue
            symbol = f"{asset}{self.quote}"
            if symbol not in self.universe.symbols:
                # A dust balance in something not tradable in this book. Reported
                # so it is visible, but never treated as a managed position.
                log.debug("ignoring untradable balance %s %s", asset, total)
                continue
            holdings.append(
                {
                    "stk_cd": symbol,
                    "asset": asset,
                    "rmnd_qty": total,
                    "free_qty": free,
                    "locked_qty": locked,
                    "book": self.universe.book_of(symbol),
                }
            )

        prices = self.prices([h["stk_cd"] for h in holdings])
        total_value = 0.0
        for h in holdings:
            price = prices.get(h["stk_cd"], 0.0)
            h["evlt_amt"] = h["rmnd_qty"] * price
            h["cur_prc"] = price
            total_value += h["evlt_amt"]

        positions = {
            "rows": holdings,
            "tot_evlt_amt": total_value,
            # Equity = holdings + all quote currency, the denominator the gate uses.
            "prsm_dpst_aset_amt": total_value + quote_free + quote_locked,
        }
        # `ord_alow_amt` is the field the sizer reads; only free quote is spendable.
        cash = {"ord_alow_amt": quote_free, "entr": quote_free, "locked": quote_locked}
        return positions, cash

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """One call for all prices; per-symbol requests are wasteful here."""
        if not symbols:
            return {}
        rows = self.client.call("price").body.get("rows", [])
        wanted = set(symbols)
        return {r["symbol"]: _f(r.get("price")) for r in rows if r.get("symbol") in wanted}

    def cost_basis(self, symbol: str) -> float:
        """Average price actually paid, reconstructed from the broker's fills.

        A Binance balance read reports quantity, never what it cost. Using the
        current price as a stand-in makes any stop derived from it trail the
        market down and never fire.
        """
        try:
            rows = self.client.call("my_trades", {"symbol": symbol, "limit": 200}).body.get(
                "rows", []
            )
        except Exception as exc:  # noqa: BLE001 - absence is reported, never guessed
            log.warning("cost basis unavailable for %s: %s", symbol, exc)
            return 0.0
        qty = quote = 0.0
        # Walk newest-first and accumulate only the buys that make up the CURRENT
        # position; earlier round trips are already closed and would skew the mean.
        for t in reversed(rows):
            if not t.get("isBuyer"):
                qty = quote = 0.0
                continue
            qty += _f(t.get("qty"))
            quote += _f(t.get("quoteQty"))
        return quote / qty if qty > 0 else 0.0

    def open_orders(self) -> dict:
        return {"rows": self.client.call("open_orders").body.get("rows", [])}

    # -- snapshots ------------------------------------------------------------

    def refresh(self, **_) -> Snapshot:
        positions, cash = self.positions_and_cash()
        self._snapshot = Snapshot(
            market=str(self.client.market),
            taken_at=dt.datetime.now(dt.UTC),
            positions=positions,
            cash=cash,
            open_orders=self.open_orders(),
            evaluation=positions,
        )
        return self._snapshot

    def current(self, **bodies) -> Snapshot:
        if self._snapshot is None or not self._snapshot.is_fresh(self.state_cfg.max_staleness_s):
            return self.refresh(**bodies)
        return self._snapshot

    def reconcile(self, **bodies) -> Snapshot:
        return self.refresh(**bodies)

    def assert_reconciled(self) -> Snapshot:
        if self.state_cfg.reconcile_before_order:
            return self.reconcile()
        snap = self._snapshot
        if snap is None:
            raise StaleStateError("no broker snapshot; call reconcile() before ordering")
        if not snap.is_fresh(self.state_cfg.max_staleness_s):
            raise StaleStateError(
                f"snapshot is {snap.age_s():.1f}s old, limit is {self.state_cfg.max_staleness_s}s"
            )
        return snap


class BinanceExecutor:
    """Transmits Binance orders. Takes a Verdict, never a bare intent."""

    def __init__(
        self,
        client: BinanceClient,
        gate,
        universe,
        cfg: AppConfig | None = None,
        *,
        dry_run: bool | None = None,
    ):
        self.cfg = cfg or config()
        self.client = client
        self.gate = gate
        self.universe = universe
        self.dry_run = self.cfg.agent.dry_run if dry_run is None else dry_run

    def _params(self, intent, rules: SymbolRules | None) -> dict:
        qty = intent.quantity
        if rules is not None:
            qty = rules.quantize_qty(qty)
        params = {
            "symbol": intent.symbol,
            "side": "BUY" if intent.side is Side.BUY else "SELL",
            "type": "LIMIT" if intent.limit_price is not None else "MARKET",
            # Formatted plainly: Binance rejects scientific notation, which is how
            # str() renders small quantities like 1e-05.
            "quantity": format(qty, "f"),
        }
        if intent.limit_price is not None:
            price = rules.quantize_price(intent.limit_price) if rules else intent.limit_price
            params["price"] = format(price, "f")
            params["timeInForce"] = "GTC"
        return params

    def execute(self, verdict: Verdict) -> dict:
        if not verdict.approved:
            from trading.brokers.kiwoom.orders import OrderRejected

            raise OrderRejected(str(verdict))

        intent = verdict.intent
        rules = self.universe.rules_for(intent.symbol)
        params = self._params(intent, rules)

        if rules is not None:
            price = intent.price_for_valuation or 0.0
            if reason := rules.rejects(rules.quantize_qty(intent.quantity), price):
                # Catch it here rather than spend a round trip on a certain reject.
                raise ValueError(f"{intent.symbol}: {reason}")

        if self.dry_run:
            log.warning("DRY RUN — not transmitting: %s", params)
            return {"dry_run": True, "params": params}

        if intent.side is Side.SELL:
            self._free_reserved_quantity(intent.symbol, intent.quantity)

        body = self.client.call("order", params).body
        self.gate.record_sent()
        log.critical("ORDER SENT %s -> %s", params, body.get("orderId"))
        return body

    def _free_reserved_quantity(self, symbol: str, needed: float) -> None:
        """Cancel resting orders holding the asset we are trying to sell.

        A resting stop reserves the balance, so a holding under protection reports
        `free: 0` and a sell is rejected for insufficient funds even though the
        asset is plainly there. Both live bStocks positions were in exactly this
        state. Cancelling is safe *because* we are about to sell: the stop exists
        to get us out, and we are getting out.
        """
        try:
            resting = [
                o
                for o in self.client.call("open_orders", {"symbol": symbol}).body.get("rows", [])
                if o.get("side") == "SELL"
            ]
        except Exception as exc:  # noqa: BLE001 - report, then let the order try
            log.error("could not list resting orders for %s: %s", symbol, exc)
            return

        reserved = sum(_f(o.get("origQty")) - _f(o.get("executedQty")) for o in resting)
        if not resting or reserved <= 0:
            return
        log.warning(
            "%s: cancelling %d resting sell order(s) reserving %.8f to free %.8f",
            symbol,
            len(resting),
            reserved,
            needed,
        )
        for order in resting:
            try:
                self.cancel(symbol, order["orderId"])
            except Exception as exc:  # noqa: BLE001 - one failure must not block the rest
                log.error("cancel failed for %s %s: %s", symbol, order.get("orderId"), exc)

    def cancel(self, symbol: str, order_id: int | str) -> dict:
        params = {"symbol": symbol, "orderId": order_id}
        if self.dry_run:
            log.warning("DRY RUN — not cancelling: %s", params)
            return {"dry_run": True, "params": params}
        return self.client.call("cancel", params).body
