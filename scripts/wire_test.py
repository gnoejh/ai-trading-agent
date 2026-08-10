"""One real, minimum-size order — the wire test.

    uv run python scripts/wire_test.py            # dry run, shows what it would send
    uv run python scripts/wire_test.py --live     # transmits ~$6, then sells it back

Why this exists: as of 2026-08-10 no order had ever left this system. Every path
was verified up to the wire and stopped there, which means the code that actually
moves money — signing, quantisation, filter validation, response parsing, the
cancel-before-sell path — had never executed once.

Finding a bug in that code with $3,136 committed is a bad way to find it. This
buys roughly $6 of BTC and sells it straight back. Cost is the round trip
(~0.2%, about one US cent) plus spread.

It deliberately uses the PRODUCTION adapter, gate and executor. A bespoke script
that talks to Binance directly would prove nothing about the code that trades.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Run from anywhere: scripts/ is not on sys.path the way the repo root is.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.brokers.adapters import build_adapter
from trading.config import load_config
from trading.risk.gate import RiskGate, Side, TradeIntent

SYMBOL = "BTCUSDT"
TARGET_NOTIONAL = 6.0  # a little over the $5 minNotional, so rounding cannot fail it


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="actually transmit")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--notional", type=float, default=TARGET_NOTIONAL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    cfg.agent.dry_run = not args.live

    adapter = build_adapter("binance", None, cfg)
    gate = RiskGate(adapter.state(), cfg, market=adapter.market)
    executor = adapter.executor(gate)
    executor.dry_run = not args.live

    rules = adapter.rules_for(args.symbol)
    price = adapter.prices([args.symbol]).get(args.symbol, 0.0)
    if not price or rules is None:
        print(f"no price or rules for {args.symbol}")
        return 1

    qty = float(rules.quantize_qty(args.notional / price))
    print(f"mode      : {'LIVE — REAL MONEY' if args.live else 'dry run'}")
    print(f"{args.symbol:<10}: price {price:,.2f}  qty {qty:g}  notional {qty * price:.2f}")
    if reason := rules.rejects(qty, price):
        print(f"would be rejected by the venue: {reason}")
        return 1

    buy = TradeIntent(
        market=adapter.market,
        side=Side.BUY,
        symbol=args.symbol,
        quantity=qty,
        reference_price=price,
    )
    verdict = gate.evaluate(buy)
    print(f"gate BUY  : {verdict.approved} {verdict.reasons}")
    if not verdict.approved:
        return 1

    resp = executor.execute(verdict)
    keys = ("orderId", "status", "executedQty", "cummulativeQuoteQty", "dry_run")
    print("BUY  resp :", {k: resp.get(k) for k in keys if k in resp})

    filled = float(resp.get("executedQty") or 0)
    if not args.live or filled <= 0:
        return 0

    # Sell it straight back. This also exercises cancel-before-sell, which is the
    # path a held position under a resting stop depends on.
    time.sleep(2)
    sell = TradeIntent(
        market=adapter.market,
        side=Side.SELL,
        symbol=args.symbol,
        quantity=float(rules.quantize_qty(filled)),
        reference_price=price,
    )
    sv = gate.evaluate(sell)
    print(f"gate SELL : {sv.approved} {sv.reasons}")
    if not sv.approved:
        print("!! position left open — sell it manually")
        return 1
    sresp = executor.execute(sv)
    print("SELL resp :", {k: sresp.get(k) for k in keys if k in sresp})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
