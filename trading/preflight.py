"""Pre-live checks.

    uv run python -m trading.preflight

Verifies everything an order depends on, and prints the account-relative size of
each risk limit so a misconfigured cap is visible before it costs money. Read-only:
it never sends an order.
"""

from __future__ import annotations

import logging

from trading.brokers.kiwoom.account import AccountState
from trading.brokers.kiwoom.client import KiwoomClient
from trading.brokers.kiwoom.orders import OrderExecutor
from trading.config import config
from trading.llm.client import LLMClient
from trading.notify.telegram import TelegramNotifier
from trading.rag.spec_parser import Market
from trading.risk.gate import RiskGate, Side, TradeIntent


def _f(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    cfg = config()
    k = cfg.broker.kiwoom
    problems: list[str] = []

    mode = "TESTNET (모의투자)" if k.use_testnet else "*** LIVE MAINNET ***"
    print(f"mode            : {mode}")
    print(f"allow_orders    : {k.allow_orders}")
    print(f"dry_run         : {cfg.agent.dry_run}")

    client = KiwoomClient(Market(cfg.agent.market))
    state = AccountState(client)
    gate = RiskGate(state, cfg)
    executor = OrderExecutor(client, gate, cfg)

    # 1. auth + broker state
    snap = state.reconcile()
    equity = _f(snap.positions.get("prsm_dpst_aset_amt")) or (
        _f(snap.cash.get("entr")) + _f(snap.positions.get("tot_evlt_amt"))
    )
    cash = _f(snap.cash.get("ord_alow_amt") or snap.cash.get("entr"))
    print(f"\nequity          : {equity:>15,.0f} KRW")
    print(f"orderable cash  : {cash:>15,.0f} KRW")
    if equity <= 0:
        problems.append("account equity reads as zero")

    # 2. limits, expressed against that equity
    r = cfg.risk
    print("\nrisk limits vs equity")
    print(
        f"  max order     : {r.max_order_value_krw:>13,.0f}  ({r.max_order_value_krw / equity:.1%} of equity)"
    )
    print(f"  max position  : {r.max_position_pct * equity:>13,.0f}  ({r.max_position_pct:.0%})")
    print(
        f"  max daily loss: {r.max_daily_loss_krw:>13,.0f}  ({r.max_daily_loss_krw / equity:.1%})"
    )
    print(f"  orders/day    : {r.max_orders_per_day:>13}")
    if r.max_order_value_krw > cash:
        problems.append(f"max_order_value_krw {r.max_order_value_krw:,.0f} exceeds orderable cash")
    if r.max_daily_loss_krw > equity * 0.1:
        problems.append("max_daily_loss_krw is over 10% of equity")

    # 3. kill switch
    print(
        f"\nkill switch     : {'SET — orders blocked' if gate.halted else 'clear'} ({gate.halt_file})"
    )

    # 4. the gate actually rejects a deliberately bad order
    absurd = TradeIntent(
        market=cfg.agent.market,
        side=Side.BUY,
        symbol="005930",
        quantity=999999,
        limit_price=100000,
    )
    if gate.evaluate(absurd).approved:
        problems.append("risk gate APPROVED an absurd order — do not trade")
    else:
        print("gate            : rejects oversized order (ok)")

    # 5. order body builds and validates against the spec, without sending
    probe = TradeIntent(
        market=cfg.agent.market, side=Side.BUY, symbol="005930", quantity=1, limit_price=50000
    )
    body = executor._body(probe)
    try:
        client.store.validate_body(executor.orders.buy, body)
        print(f"order shape     : valid for {executor.orders.buy} {body}")
    except ValueError as exc:
        problems.append(f"order body invalid: {exc}")

    # 6. models and notifications
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
    if not k.use_testnet and not cfg.agent.dry_run:
        print("\n*** LIVE TRADING ENABLED — real orders will be sent with real money. ***")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
