"""What execution actually costs, measured from the mainnet order book.

The hurdle every trade must clear is `commission x 2 + slippage x 2`, and on the
crypto book slippage is **60% of it** -- 0.30% of the 0.500% round trip, against
0.20% of actual commission. That 15 bps/side was set when the screen bought
microcap breakouts. Since 2026-09-09 the screen buys BTC/ETH/SOL/AVAX/XRP, and
nothing has re-measured the number since. It matters out of proportion to its
size: the hurdle sets net break-even, which sets every exit level, which decides
whether a closed trip is scored a win -- so an overstated slippage assumption
does not merely tax the P&L report, it moves the stops and the targets and
teaches the learning loop that trades were losses.

**Measured against MAINNET depth, never against fills.** Two reasons, and they
are the same reason twice: the testnet's books are bot-seeded, so its fills are
fantasy prices (the 2026-08-30 data/trade-plane split exists for exactly this),
and the hurdle is a claim about mainnet execution. `depth` is unsigned, so it
reads the mainnet data plane even while `use_testnet` is on.

Three decisions worth keeping:

1. **Slippage is a property of (symbol, notional), not of a symbol.** A $100
   order on BTCUSDT and a $100,000 one are different questions, so the probe
   sweeps `notionals` rather than reporting one number.
2. **The MEDIAN is the headline, not the mean.** One illiquid name in a
   25-symbol menu drags an average anywhere; this repo already learned that on
   2026-09-09, when a band scoring +15.49% average read -4.03% median and turned
   out to be a lottery.
3. **A book too thin to fill the order is a RESULT, not an error.** It is
   reported as `insufficient` and excluded from the statistics rather than
   silently clamped to the last level -- a clamp would report the thinnest names
   as the cheapest.

The reference is the book mid, and the half-spread is part of the cost: a market
buy crosses it. Lot-size quantisation is not modelled (it moves the notional by
less than one tick of depth).

Run: `uv run python -m trading.accounting.slippage`
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.universe import BinanceScreen, BinanceUniverse
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Fill:
    """One simulated market order walked through the book."""

    symbol: str
    book: str
    side: str
    notional: float
    vwap: float
    mid: float
    slippage_bps: float
    filled_pct: float

    @property
    def insufficient(self) -> bool:
        return self.filled_pct < 0.999


def walk(levels: list[list[str]], notional: float) -> tuple[float, float]:
    """Consume `notional` of quote currency against one side of the book.

    Returns (vwap, filled_fraction). Levels arrive as Binance sends them --
    `[["price", "qty"], ...]`, asks ascending and bids descending, i.e. already
    in the order a market order would eat them.
    """
    spent = 0.0
    units = 0.0
    for raw_price, raw_qty in levels:
        price, qty = float(raw_price), float(raw_qty)
        if price <= 0 or qty <= 0:
            continue
        room = notional - spent
        if room <= 0:
            break
        take = min(qty, room / price)
        spent += take * price
        units += take
    if units <= 0:
        return 0.0, 0.0
    return spent / units, min(1.0, spent / notional)


class SlippageProbe:
    """Prices a round trip against the live mainnet book."""

    def __init__(self, cfg: AppConfig | None = None, client: BinanceClient | None = None):
        self.cfg = cfg or config()
        self.pcfg = self.cfg.accounting.slippage_probe
        self.client = client or BinanceClient("CRYPTO", cfg=self.cfg)

    def depth(self, symbol: str) -> dict:
        return self.client.call("depth", {"symbol": symbol, "limit": self.pcfg.depth_limit}).body

    def measure(self, symbol: str, book: str, notional: float) -> list[Fill]:
        rows = self.depth(symbol)
        bids, asks = rows.get("bids") or [], rows.get("asks") or []
        if not bids or not asks:
            log.warning("%s: empty book side, skipped", symbol)
            return []
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        out = []
        for side, levels in (("BUY", asks), ("SELL", bids)):
            vwap, filled = walk(levels, notional)
            # Signed so a cost is POSITIVE on both sides: a buy fills above mid,
            # a sell below it.
            drift = (vwap - mid) if side == "BUY" else (mid - vwap)
            out.append(
                Fill(
                    symbol=symbol,
                    book=book,
                    side=side,
                    notional=notional,
                    vwap=vwap,
                    mid=mid,
                    slippage_bps=drift / mid * 10_000 if filled else 0.0,
                    filled_pct=filled,
                )
            )
        return out

    def population(self, which: str) -> list[dict]:
        """`menu` is what the model is offered; `pool` is what the dice draw from.

        Both are measured because the random arm opens most of the entries: a
        hurdle tuned on the model's menu alone would be wrong for two thirds of
        the book.
        """
        universe = BinanceUniverse(self.client, list(self.cfg.broker.binance.markets))
        screen = BinanceScreen(self.client, universe, self.cfg)
        rows = screen.candidates() if which == "menu" else screen.tradable_pool()
        by_book: dict[str, list[dict]] = {}
        for row in rows:
            by_book.setdefault(row["book"], []).append(row)
        out: list[dict] = []
        for entries in by_book.values():
            ranked = sorted(entries, key=lambda e: -e["quote_volume"])
            # The pool is the RANDOM arm's population, so it is sampled across
            # the liquidity range rather than skimmed off the top: taking the
            # most-traded N would measure a book the dice never draw from.
            if which == "pool" and len(ranked) > self.pcfg.symbols_per_book:
                step = len(ranked) / self.pcfg.symbols_per_book
                ranked = [ranked[int(i * step)] for i in range(self.pcfg.symbols_per_book)]
            out.extend(ranked[: self.pcfg.symbols_per_book])
        return out

    def run(self, which: str = "menu") -> dict:
        rows = self.population(which)
        log.info("probing %d symbols (%s)", len(rows), which)
        fills: list[Fill] = []
        for row in rows:
            for notional in self.pcfg.notionals:
                fills.extend(self.measure(row["symbol"], row["book"], notional))
        return {
            "population": which,
            "symbols": sorted({f.symbol for f in fills}),
            "cells": self._summarise(fills),
            "fills": [asdict(f) for f in fills],
        }

    def _summarise(self, fills: list[Fill]) -> list[dict]:
        """One row per (book, notional): the round-trip slippage a trade pays."""
        cells: dict[tuple[str, float], list[Fill]] = {}
        for f in fills:
            cells.setdefault((f.book, f.notional), []).append(f)
        out = []
        for (book, notional), group in sorted(cells.items()):
            thin = sorted({f.symbol for f in group if f.insufficient})
            # A round trip pays both sides, which is how round_trip_rate reads it.
            per_symbol: dict[str, float] = {}
            for f in group:
                if f.symbol in thin:
                    continue
                per_symbol[f.symbol] = per_symbol.get(f.symbol, 0.0) + f.slippage_bps
            values = sorted(per_symbol.values())
            configured = self.cfg.accounting.fees_for(book).slippage_bps * 2
            median = statistics.median(values) if values else None
            out.append(
                {
                    "book": book,
                    "notional": notional,
                    "n_symbols": len(values),
                    "round_trip_bps_median": round(median, 2) if median is not None else None,
                    "round_trip_bps_mean": (round(statistics.fmean(values), 2) if values else None),
                    "round_trip_bps_worst": round(values[-1], 2) if values else None,
                    "configured_round_trip_bps": configured,
                    "overstated_x": (
                        round(configured / median, 2) if median and median > 0 else None
                    ),
                    "insufficient_depth": thin,
                }
            )
        return out


def render(report: dict) -> str:
    lines = [
        f"*Execution slippage* (mainnet book, population: {report['population']})",
        f"  {len(report['symbols'])} symbols · round-trip bps = both sides crossed",
    ]
    for cell in report["cells"]:
        med = cell["round_trip_bps_median"]
        if med is None:
            lines.append(f"  {cell['book']:8} ${cell['notional']:>7,.0f}  no fillable symbols")
            continue
        over = cell["overstated_x"]
        verdict = f"config assumes {cell['configured_round_trip_bps']:.0f}"
        if over and over > 1:
            verdict += f" — overstated {over:.1f}x"
        elif over:
            verdict += f" — UNDERSTATED, real cost is {1 / over:.1f}x that"
        lines.append(
            f"  {cell['book']:8} ${cell['notional']:>7,.0f}  "
            f"median {med:7.2f}  mean {cell['round_trip_bps_mean']:7.2f}  "
            f"worst {cell['round_trip_bps_worst']:8.2f}  n={cell['n_symbols']:2}  {verdict}"
        )
        if cell["insufficient_depth"]:
            lines.append(
                f"           ↳ too thin to fill: {', '.join(cell['insufficient_depth'][:6])}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Measure execution slippage from the mainnet book.")
    ap.add_argument(
        "--population",
        choices=("menu", "pool", "both"),
        default="both",
        help="menu = the model's candidates; pool = what the random arm draws from",
    )
    args = ap.parse_args(argv)

    cfg = config()
    probe = SlippageProbe(cfg)
    which = ("menu", "pool") if args.population == "both" else (args.population,)
    reports = [probe.run(w) for w in which]
    for report in reports:
        print(render(report))
        print()
    out = Path(cfg.accounting.slippage_probe.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
