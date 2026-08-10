"""Laddered entries and exits — rungs.

A single all-in order takes exactly one price, and that price is whatever the
market happened to be at the instant a model finished thinking. A ladder spreads
the same capital across several levels, so the average fill is a weighted average
of a range rather than a bet on one tick.

Two things about rungs are commonly assumed and wrong, so they are stated here:

* **Rungs do not cost more in commission.** Fees are proportional to notional, so
  four buys of $750 pay exactly what one buy of $3,000 pays. What rungs do change
  is the *minimum size* constraint: each rung is a separate order and must clear
  `minNotional` on its own, which is what makes a 6-rung ladder impossible on a
  small balance.
* **Exit rungs hang off NET BREAK-EVEN, not entry.** Laddering out from the entry
  price sells the first rung at a loss after costs. Every exit level here is a
  multiple of the round-trip hurdle above the price at which the position is
  genuinely flat.

Entry rungs are placed at and below the reference price: the first takes a
position immediately, and the rest improve the average only if the price comes to
them. Unfilled rungs are not failures — they are the ladder declining to pay up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from trading.config import AppConfig, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Rung:
    """One level of a ladder."""

    index: int
    price: float
    quantity: float
    weight: float
    side: str  # BUY | SELL

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def __str__(self) -> str:
        return f"rung {self.index} {self.side} {self.quantity:g} @ {self.price:,.6g} = {self.notional:,.2f}"


def _normalise(weights: list[float], count: int) -> list[float]:
    """Weights that sum to 1, padded or truncated to `count`."""
    if not weights:
        weights = [1.0] * count
    weights = list(weights[:count]) + [weights[-1]] * max(0, count - len(weights))
    total = sum(weights) or 1.0
    return [w / total for w in weights]


class RungPlanner:
    def __init__(self, cfg: AppConfig | None = None, ledger=None):
        self.cfg = cfg or config()
        self.rcfg = self.cfg.rungs
        if ledger is None:
            from trading.accounting.costs import CostLedger

            ledger = CostLedger(self.cfg)
        self.ledger = ledger

    # -- entries --------------------------------------------------------------

    def entry_rungs(
        self, price: float, budget: float, rules=None, market: str | None = None
    ) -> list[Rung]:
        """Split `budget` into buy levels at and below `price`.

        Rungs that cannot clear the venue's minimum order size are dropped and
        their weight is NOT redistributed -- silently fattening the remaining
        rungs would defeat the point of laddering.
        """
        if price <= 0 or budget <= 0:
            return []
        cfg = self.rcfg.entry
        weights = _normalise(cfg.weights, cfg.count)

        out: list[Rung] = []
        for i, weight in enumerate(weights):
            # Rung 0 sits at the reference price; each subsequent rung steps down.
            level = price * (1 - cfg.start_offset_pct - cfg.spacing_pct * i)
            if level <= 0:
                continue
            qty = (budget * weight) / level
            if rules is not None:
                level = float(rules.quantize_price(level))
                qty = float(rules.quantize_qty(qty))
                if reason := rules.rejects(Decimal(str(qty)), Decimal(str(level))):
                    log.info("entry rung %d dropped: %s", i, reason)
                    continue
            else:
                qty = float(int(qty))  # whole shares
                if qty <= 0:
                    log.info("entry rung %d dropped: rounds to zero shares", i)
                    continue
            out.append(Rung(index=i, price=level, quantity=qty, weight=weight, side="BUY"))
        return out

    # -- exits ----------------------------------------------------------------

    def exit_rungs(
        self, net_breakeven: float, quantity: float, rules=None, market: str | None = None
    ) -> list[Rung]:
        """Scale out above net break-even, never above entry.

        The first level already clears costs, so every rung is a real gain rather
        than a nominal one.
        """
        if net_breakeven <= 0 or quantity <= 0:
            return []
        cfg = self.rcfg.exit
        weights = _normalise(cfg.weights, cfg.count)
        hurdle = self.ledger.breakeven_move_pct(market)

        out: list[Rung] = []
        remaining = quantity
        for i, weight in enumerate(weights):
            # Each rung sits a further multiple of the cost hurdle above break-even.
            step = cfg.first_hurdle_multiple + cfg.spacing_hurdle_multiple * i
            level = net_breakeven * (1 + step * hurdle)
            qty = quantity * weight
            if i == len(weights) - 1:
                # Last rung takes the remainder so quantisation cannot strand dust.
                qty = remaining
            if rules is not None:
                level = float(rules.quantize_price(level))
                qty = float(rules.quantize_qty(qty))
                if reason := rules.rejects(Decimal(str(qty)), Decimal(str(level))):
                    log.info("exit rung %d dropped: %s", i, reason)
                    continue
            else:
                qty = float(int(qty))
                if qty <= 0:
                    continue
            remaining = max(0.0, remaining - qty)
            out.append(Rung(index=i, price=level, quantity=qty, weight=weight, side="SELL"))
        return out

    # -- diagnostics ----------------------------------------------------------

    def describe(self, rungs: list[Rung], market: str | None = None) -> str:
        if not rungs:
            return "no rungs (budget too small for the venue's minimum order size)"
        total = sum(r.notional for r in rungs)
        avg = total / sum(r.quantity for r in rungs)
        lines = [str(r) for r in rungs]
        lines.append(f"total {total:,.2f} across {len(rungs)} rungs, average price {avg:,.6g}")
        return "\n".join(lines)
