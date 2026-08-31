"""Pre-live checks.

    uv run python -m trading.preflight

Verifies everything an order depends on, and prints the account-relative size of
each risk limit so a misconfigured cap is visible before it costs money. Read-only:
it never sends an order.
"""

from __future__ import annotations

import logging

from trading.brokers.adapters import BinanceAdapter
from trading.config import config
from trading.llm.client import LLMClient
from trading.notify.telegram import TelegramNotifier
from trading.risk.gate import RiskGate, Side, TradeIntent


def _f(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    cfg = config()
    b = cfg.broker.binance
    problems: list[str] = []

    mode = "TESTNET" if b.use_testnet else "*** LIVE MAINNET ***"
    print(f"mode            : {mode}")
    print(f"allow_orders    : {b.allow_orders}")
    print(f"dry_run         : {cfg.agent.dry_run}")

    adapter = BinanceAdapter(cfg=cfg)
    state = adapter.state()
    gate = RiskGate(state, cfg, market="BINANCE")

    # 1. auth + broker state
    snap = state.reconcile()
    equity = _f(snap.positions.get("prsm_dpst_aset_amt"))
    cash = _f(snap.cash.get("ord_alow_amt") or snap.cash.get("entr"))
    print(f"\nequity          : {equity:>15,.2f} USDT")
    print(f"orderable cash  : {cash:>15,.2f} USDT")
    if equity <= 0:
        problems.append("account equity reads as zero")

    # 2. limits, expressed against that equity
    r = cfg.risk
    print("\nrisk limits vs equity")
    if equity > 0:
        print(
            f"  max order     : {r.max_order_value_krw:>13,.0f}"
            f"  ({r.max_order_value_krw / equity:.1%} of equity)"
            if r.max_order_value_krw
            else "  max order     :     unlimited"
        )
        print(
            f"  max position  : {r.max_position_pct * equity:>13,.0f}  ({r.max_position_pct:.0%})"
            if r.max_position_pct
            else "  max position  :     unlimited"
        )
        print(
            f"  max daily loss: {r.max_daily_loss_pct * equity:>13,.0f}"
            f"  ({r.max_daily_loss_pct:.0%} of equity)"
            if r.max_daily_loss_pct
            else "  max daily loss:     unlimited"
        )
    print(f"  orders/day    : {r.max_orders_per_day or 'unlimited':>13}")

    # 3. kill switch
    print(
        f"\nkill switch     : {'SET — orders blocked' if gate.halted else 'clear'} ({gate.halt_file})"
    )

    # 4. the gate actually rejects a deliberately bad order
    absurd = TradeIntent(
        market="BINANCE",
        side=Side.BUY,
        symbol="BTCUSDT",
        quantity=999999,
        limit_price=100000,
    )
    if gate.evaluate(absurd).approved:
        problems.append("risk gate APPROVED an absurd order — do not trade")
    else:
        print("gate            : rejects oversized order (ok)")

    # 5. models and notifications
    try:
        LLMClient().ask("say ok", tier=cfg.agent.tiers.decide)
        print(f"llm ({cfg.agent.tiers.decide:<10}): reachable")
    except Exception as exc:  # noqa: BLE001 - a report, not a control path
        problems.append(f"llm tier {cfg.agent.tiers.decide} unreachable: {exc}")
    tg = TelegramNotifier()
    print(f"telegram        : {'configured' if tg.configured else 'NOT configured'}")
    if not tg.configured:
        problems.append("telegram not configured — you would be trading unobserved")

    print()
    if problems:
        for p in problems:
            print(f"  BLOCK: {p}")
        print(f"\n{len(problems)} problem(s). Not ready.")
        return 1
    print("preflight OK.")
    if not b.use_testnet and not cfg.agent.dry_run:
        print("\n*** LIVE TRADING ENABLED — real orders will be sent with real money. ***")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
