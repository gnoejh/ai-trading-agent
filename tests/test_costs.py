"""Cost accounting tests.

Profit is realised P&L minus trading fees minus API spend. These pin the
arithmetic that decides whether the system is actually making money.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trading.accounting.costs import CostLedger, Usage
from trading.config import load_config


@pytest.fixture
def ledger(tmp_path):
    cfg = load_config()
    cfg.accounting.ledger = str(tmp_path / "ledger.jsonl")
    cfg.accounting.fees.commission_rate = 0.00015
    cfg.accounting.fees.sell_tax_rate = 0.0015
    cfg.accounting.fees.slippage_bps = 5
    cfg.llm.usd_krw = 1380.0
    return CostLedger(cfg)


def test_buy_pays_commission_and_slippage_but_no_tax(ledger):
    # 1,000,000 * (0.00015 + 0.0005) = 650
    assert ledger.trade_fee(1_000_000, side="BUY") == pytest.approx(650.0)


def test_sell_additionally_pays_transaction_tax(ledger):
    # 1,000,000 * (0.00015 + 0.0005 + 0.0015) = 2,150
    assert ledger.trade_fee(1_000_000, side="SELL") == pytest.approx(2150.0)


def test_round_trip_rate_is_the_hurdle(ledger):
    # buy 650 + sell 2,150 = 2,800 on 1,000,000 = 0.28%
    assert ledger.round_trip_cost(1_000_000) == pytest.approx(2800.0)
    assert ledger.breakeven_move_pct() == pytest.approx(0.0028)


def test_llm_call_is_priced_from_config(ledger):
    # deepseek-chat: 0.27 in / 1.10 out per 1M
    usd = ledger.price_call("deepseek-chat", 1_000_000, 1_000_000)
    assert usd == pytest.approx(1.37)


def test_unknown_model_costs_zero_rather_than_guessing(ledger):
    assert ledger.price_call("some-unreleased-model", 1000, 1000) == 0.0


def test_day_aggregates_api_and_fees(ledger):
    ledger.record_llm(
        Usage(
            model="deepseek-chat",
            provider="deepseek",
            tier="deep",
            input_tokens=100_000,
            output_tokens=10_000,
            usd=ledger.price_call("deepseek-chat", 100_000, 10_000),
        )
    )
    ledger.record_trade(symbol="005930", side="BUY", quantity=10, price=70_000)
    ledger.record_trade(symbol="005930", side="SELL", quantity=10, price=71_000)

    day = ledger.day()
    assert day.llm_calls == 1
    assert day.trades == 2
    assert day.input_tokens == 100_000
    # 700,000 buy -> 455 ; 710,000 sell -> 1,526.5
    assert day.trade_fees_krw == pytest.approx(455.0 + 1526.5)
    assert day.api_krw > 0
    assert day.total_cost_krw == pytest.approx(day.api_krw + day.trade_fees_krw)


def test_net_is_realised_minus_every_cost(ledger):
    ledger.record_trade(symbol="005930", side="BUY", quantity=10, price=70_000)
    ledger.record_realised(10_000)
    day = ledger.day()
    assert day.net_krw == pytest.approx(10_000 - day.total_cost_krw)
    assert day.breakeven_gap_krw == 0.0


def test_breakeven_gap_when_behind(ledger):
    ledger.record_trade(symbol="005930", side="SELL", quantity=100, price=70_000)
    ledger.record_realised(1_000)
    day = ledger.day()
    assert day.net_krw < 0
    assert day.breakeven_gap_krw == pytest.approx(day.total_cost_krw - 1_000)


def test_realised_is_replaced_not_accumulated(ledger):
    """The broker reports a running total; summing it would double-count."""
    ledger.record_realised(5_000)
    ledger.record_realised(8_000)
    assert ledger.day().realised_pl_krw == 8_000


def test_a_winning_trade_can_still_be_a_net_loss(ledger):
    """The point of the whole module: gross profit is not profit."""
    notional = 700_000
    ledger.record_trade(symbol="005930", side="BUY", quantity=10, price=70_000)
    ledger.record_trade(symbol="005930", side="SELL", quantity=10, price=70_100)  # +1,000 gross
    ledger.record_realised(1_000)
    day = ledger.day()
    assert day.realised_pl_krw > 0
    assert day.net_krw < 0, "0.14% gain does not clear a 0.28% round trip"
    assert ledger.round_trip_cost(notional) > 1_000


def test_report_renders(ledger):
    ledger.record_realised(0)
    text = ledger.breakeven(dt.datetime.now(dt.UTC).date())
    assert "Break-even" in text and "round trip costs" in text


def test_owner_flows_are_never_counted_as_performance(ledger):
    """A deposit must not read as a gain, nor a withdrawal as a loss."""
    ledger.record_realised(5_000)
    ledger.record_cash_flow(1_000_000, "deposit")
    ledger.record_cash_flow(-200_000, "withdrawal")
    day = ledger.day()
    assert day.realised_pl_krw == 5_000, "flows must not touch realised P&L"
    assert day.net_krw == pytest.approx(5_000 - day.total_cost_krw)
    assert ledger.cash_flows() == pytest.approx(800_000)
