"""Position sizing — how much of the balance one entry commits.

In ``full_balance`` mode an entry takes the whole orderable balance, so the model
proposes *what* to buy and this decides *how much*. Quantity is not left to the
model: it is arithmetic over broker-reported cash, and arithmetic does not
hallucinate a number with an extra digit.

Two details are settlement mechanics rather than policy, and getting them wrong
produces orders the broker simply rejects:

* **Orderable, not total.** KR equities settle T+2, so 추정예탁자산 includes sale
  proceeds that cannot fund an order yet. Sizing uses 주문가능금액.
* **A reserve.** Committing to the last won leaves nothing for the tick of price
  movement between sizing and fill, and a market order that prices up is then
  rejected outright rather than partially filled.
"""

from __future__ import annotations

import logging
import math

from trading.config import AppConfig, config

log = logging.getLogger(__name__)


def _f(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


class PositionSizer:
    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.scfg = self.cfg.sizing

    def orderable_cash(self, cash: dict) -> float:
        """Spendable-today balance, per the broker."""
        primary = _f(cash.get(self.scfg.cash_field))
        if primary > 0:
            return primary
        return _f(cash.get(self.scfg.cash_fallback_field))

    def budget(self, cash: dict) -> float:
        """Cash this entry may commit, after reserve and mode fraction."""
        available = self.orderable_cash(cash)
        if available <= 0:
            return 0.0
        fraction = 1.0 if self.scfg.mode == "full_balance" else self.scfg.fraction
        return available * fraction * (1 - self.scfg.reserve_pct)

    def quantity_for(self, price: float, cash: dict, rules=None) -> float:
        """Quantity affordable at `price`, quantised to what the venue accepts.

        KRX trades whole shares; Binance quantises per symbol (BTCUSDT steps by
        0.00001, SHIBUSDT by 1) and rejects anything under `minNotional`. Passing
        the symbol's rules makes the sizer produce an order the venue will take
        rather than one it will bounce.
        """
        if price <= 0:
            log.warning("cannot size at price %r", price)
            return 0
        budget = self.budget(cash)

        if rules is not None:
            qty = rules.quantize_qty(budget / price)
            if reason := rules.rejects(qty, price):
                log.warning("%s: %s", rules.symbol, reason)
                return 0
            return float(qty)

        lot = max(self.scfg.lot_size, 1)
        qty = math.floor(budget / price)
        qty -= qty % lot
        return max(qty, 0)

    def size(self, intent, cash: dict, rules=None) -> float:
        """Set `intent.quantity` from the balance. Returns the quantity used."""
        price = intent.price_for_valuation
        if price is None:
            log.warning("%s has no price; cannot size", intent.symbol)
            intent.quantity = 0
            return 0
        qty = self.quantity_for(price, cash, rules)
        if qty != intent.quantity:
            log.info(
                # %g, not %.0f: sub-cent crypto prices render as "0" otherwise,
                # which makes a correct order look like a divide-by-zero in the log.
                "%s sized %s -> %g at %g (budget %.2f)",
                intent.symbol,
                intent.quantity,
                qty,
                price,
                self.budget(cash),
            )
        intent.quantity = qty
        return qty

    def slots_free(self, holdings_count: int) -> int:
        """How many more positions may be opened. 0 disables the limit."""
        cap = self.scfg.max_positions
        if not cap:
            return 1_000_000
        return max(cap - holdings_count, 0)
