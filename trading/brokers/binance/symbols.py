"""Binance symbol rules — the filters an order must satisfy to be accepted.

Unlike KRX, where everything trades in whole shares, Binance sets per-symbol
quantisation and a minimum order value. Violating either is a hard rejection:

* ``LOT_SIZE.stepSize`` — quantity must be a multiple of it. BTCUSDT steps by
  0.00001, SHIBUSDT by 1, NVDABUSDT by 0.001. A sizer that emits whole units
  produces an invalid order on one symbol and a wildly oversized one on another.
* ``NOTIONAL.minNotional`` — price x quantity must clear it ($5 on BTCUSDT). This
  is what makes "spend the remaining dust" silently fail.
* ``PRICE_FILTER.tickSize`` — limit prices must be a multiple of it.

Quantisation always rounds **down** for quantity: rounding up can exceed the
balance and be rejected for insufficient funds, which is a worse failure than
buying fractionally less.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SymbolRules:
    symbol: str
    base_asset: str = ""
    quote_asset: str = ""
    step_size: Decimal = Decimal(0)
    min_qty: Decimal = Decimal(0)
    tick_size: Decimal = Decimal(0)
    min_notional: Decimal = Decimal(0)
    permissions: frozenset[str] = frozenset()

    # -- quantisation ---------------------------------------------------------

    def quantize_qty(self, quantity: float | Decimal) -> Decimal:
        """Round quantity DOWN to a valid step. Rounding up risks a balance reject."""
        q = Decimal(str(quantity))
        if self.step_size > 0:
            q = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size
        return q.normalize() if q else Decimal(0)

    def quantize_price(self, price: float | Decimal) -> Decimal:
        p = Decimal(str(price))
        if self.tick_size > 0:
            p = (p / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size
        return p

    # -- validation -----------------------------------------------------------

    def rejects(self, quantity: float | Decimal, price: float | Decimal) -> str | None:
        """Why the exchange would reject this order, or None if it is valid."""
        q, p = Decimal(str(quantity)), Decimal(str(price))
        if q <= 0:
            return "quantity is zero after quantisation"
        if self.min_qty and q < self.min_qty:
            return f"quantity {q} below minQty {self.min_qty}"
        if self.step_size and (q % self.step_size) != 0:
            return f"quantity {q} is not a multiple of stepSize {self.step_size}"
        if self.min_notional and (q * p) < self.min_notional:
            return f"notional {q * p:.4f} below minNotional {self.min_notional}"
        return None

    def is_valid(self, quantity: float | Decimal, price: float | Decimal) -> bool:
        return self.rejects(quantity, price) is None


def _dec(value) -> Decimal:
    # Decimal raises InvalidOperation (an ArithmeticError, NOT ValueError) for
    # unparseable text, so a missing filter field would otherwise escape here.
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (TypeError, ArithmeticError):
        return Decimal(0)


def parse_symbol(row: dict) -> SymbolRules:
    """Build rules from one `exchangeInfo` symbol entry."""
    filters = {f.get("filterType"): f for f in row.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    # Binance renamed MIN_NOTIONAL to NOTIONAL; both appear in the wild.
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    price = filters.get("PRICE_FILTER", {})
    return SymbolRules(
        symbol=row["symbol"],
        base_asset=row.get("baseAsset", ""),
        quote_asset=row.get("quoteAsset", ""),
        step_size=_dec(lot.get("stepSize")),
        min_qty=_dec(lot.get("minQty")),
        tick_size=_dec(price.get("tickSize")),
        min_notional=_dec(notional.get("minNotional")),
        permissions=frozenset(tag for group in row.get("permissionSets", [[]]) for tag in group),
    )


class SymbolBook:
    """All tradable symbols for one Binance book, with their filters."""

    def __init__(self, client, market_cfg):
        self.client = client
        self.market_cfg = market_cfg
        self._rules: dict[str, SymbolRules] = {}

    def load(self, *, force: bool = False) -> dict[str, SymbolRules]:
        if self._rules and not force:
            return self._rules
        rows = self.client.call("exchange_info").body.get("symbols", [])
        want, avoid = self.market_cfg.permission_tag, self.market_cfg.exclude_permission_tag
        out: dict[str, SymbolRules] = {}
        for row in rows:
            if row.get("status") != "TRADING":
                continue
            if row.get("quoteAsset") != self.market_cfg.quote_asset:
                continue
            rules = parse_symbol(row)
            # Tag-based, never suffix-based: NVDAB/CRCLB/TSLAB end in B, and so do
            # BNB, SHIB, ARB and CKB, which are ordinary coins.
            if want and want not in rules.permissions:
                continue
            if avoid and avoid in rules.permissions:
                continue
            out[rules.symbol] = rules
        self._rules = out
        log.info("binance %s: %d tradable symbols", self.market_cfg.quote_asset, len(out))
        return out

    def get(self, symbol: str) -> SymbolRules | None:
        return self.load().get(symbol)

    @property
    def symbols(self) -> set[str]:
        return set(self.load())
