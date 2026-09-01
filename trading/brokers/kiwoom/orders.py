"""Order transmission. The only code that sends a Kiwoom order.

:meth:`OrderExecutor.execute` takes a :class:`Verdict`, not a
:class:`TradeIntent`, so an approved decision from the risk gate is structurally
required to reach the broker -- there is no overload that accepts a bare intent.

`dry_run` stops at the wire: the intent is gated, journalled and reported exactly
as a live order would be, but nothing is transmitted.
"""

from __future__ import annotations

import logging

from trading.brokers.kiwoom.client import KiwoomClient
from trading.brokers.state import OrderRejected
from trading.config import AppConfig, config
from trading.risk.gate import RiskGate, Side, TradeIntent, Verdict

__all__ = ["TRADE_TYPE_LIMIT", "TRADE_TYPE_MARKET", "OrderExecutor", "OrderRejected"]

log = logging.getLogger(__name__)

# 매매구분: 0 limit (보통), 3 market (시장가).
TRADE_TYPE_LIMIT = "0"
TRADE_TYPE_MARKET = "3"


class OrderExecutor:
    """Sends Kiwoom orders. Required fields come from the parsed spec per market."""

    def __init__(
        self,
        client: KiwoomClient,
        gate: RiskGate,
        cfg: AppConfig | None = None,
        *,
        dry_run: bool | None = None,
    ):
        self.cfg = cfg or config()
        self.client = client
        self.gate = gate
        market_cfg = self.cfg.broker.kiwoom.market(client.market)
        self.orders = market_cfg.orders
        self.exchange = market_cfg.exchange
        self.dry_run = self.cfg.agent.dry_run if dry_run is None else dry_run
        self._universe = None

    def _body(self, intent: TradeIntent) -> dict:
        spec = self.client.store.get(
            self.orders.buy if intent.side is Side.BUY else self.orders.sell
        )
        required = set(spec.required_body())
        body = {
            "stk_cd": intent.symbol,
            "ord_qty": str(intent.quantity),
            "trde_tp": TRADE_TYPE_LIMIT if intent.limit_price is not None else TRADE_TYPE_MARKET,
        }
        # KR orders take 국내거래소구분; US orders take 거래소구분 with the listing's
        # own venue. Sending the KR field to ust20000 failed two valid US trades
        # (SNDK, HTZ) with "missing required field(s) stex_tp".
        if "dmst_stex_tp" in required:
            body["dmst_stex_tp"] = self.exchange
        if "stex_tp" in required:
            body["stex_tp"] = self.us_exchange_of(intent.symbol)
        for field in ("ord_dvsn", "ord_cnd"):
            if field in required and field not in body:
                body[field] = "0"
        if intent.limit_price is not None:
            body["ord_uv"] = str(int(intent.limit_price))
        return body

    def us_exchange_of(self, symbol: str) -> str:
        """The listing's own exchange (NY/ND/NA), which US orders require."""
        if self._universe is None:
            from trading.agent.universe import Universe

            self._universe = Universe(self.client, self.cfg)
        return self._universe.exchange_of(symbol) or "%"

    def execute(self, verdict: Verdict) -> dict:
        """Transmit an approved order. Rejects anything the gate did not approve."""
        if not verdict.approved:
            raise OrderRejected(str(verdict))

        intent = verdict.intent
        api_id = self.orders.buy if intent.side is Side.BUY else self.orders.sell
        body = self._body(intent)

        if self.dry_run:
            log.warning("DRY RUN — not transmitting: %s %s", api_id, body)
            return {"dry_run": True, "api_id": api_id, "body": body}

        page = self.client.call(api_id, body)
        # Count only what actually went out, so a failed send does not consume budget.
        self.gate.record_sent()
        log.critical("ORDER SENT %s %s -> %s", api_id, body, page.body.get("ord_no"))
        return page.body

    def cancel(self, order_no: str, symbol: str, quantity: int) -> dict:
        """Cancel a resting order. Not gated: reducing exposure is always allowed."""
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": order_no,
            "stk_cd": symbol,
            "cncl_qty": str(quantity),
        }
        if self.dry_run:
            log.warning("DRY RUN — not cancelling: %s", body)
            return {"dry_run": True, "body": body}
        return self.client.call(self.orders.cancel, body).body
