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
    # KRW-denominated default book: these tests assert the KR-era arithmetic,
    # where the quote currency IS the report currency and nothing converts.
    cfg.accounting.fees.currency = "KRW"
    cfg.llm.usd_krw = 1380.0
    cfg.llm.usd_cny = 7.15
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
    """DeepSeek bills this account in CNY, from a CNY list that is NOT market-FX
    of the USD list -- the ledger must convert the native CNY price itself."""
    # deepseek-v4-flash: ¥3 in / ¥9 out per 1M (peak cache-miss) -> USD at usd_cny
    usd = ledger.price_call("deepseek-v4-flash", 1_000_000, 1_000_000)
    assert usd == pytest.approx((3.00 + 9.00) / 7.15)
    # Legacy alias stays priced so a tier rollback never bills at zero.
    assert ledger.price_call("deepseek-chat", 1_000_000, 1_000_000) > 0
    # A USD-priced model converts nothing.
    assert ledger.price_call("qwen-plus", 1_000_000, 1_000_000) == pytest.approx(1.60)


def test_cny_priced_call_lands_in_krw_at_the_market_rate(ledger):
    """The user-facing number is KRW; for a CNY bill it must be CNY x (krw/cny),
    not the USD list converted at market FX (~5% apart)."""
    usd = ledger.price_call("deepseek-v4-flash", 1_000_000, 0)  # ¥3.00
    ledger.record_llm(
        Usage(
            model="deepseek-v4-flash",
            provider="deepseek",
            tier="fast",
            input_tokens=1_000_000,
            output_tokens=0,
            usd=usd,
        )
    )
    krw = ledger.day().api_krw
    assert krw == pytest.approx(3.00 / 7.15 * 1380.0, rel=1e-3)  # ≈ ¥3 x 193 KRW/CNY


def test_unknown_model_costs_zero_rather_than_guessing(ledger):
    assert ledger.price_call("some-unreleased-model", 1000, 1000) == 0.0


def test_llm_spend_is_aggregated_per_model(ledger):
    """The tier mix is a cost decision (one reasoner call ~5x a chat call), so
    the operator report breaks spend down by model, not just in total."""
    for model, out_tokens in (
        ("deepseek-chat", 500),
        ("deepseek-chat", 700),
        ("deepseek-reasoner", 9000),
    ):
        ledger.record_llm(
            Usage(
                model=model,
                provider="deepseek",
                tier="fast",
                input_tokens=4000,
                output_tokens=out_tokens,
                usd=ledger.price_call(model, 4000, out_tokens),
            )
        )
    by_model = ledger.llm_by_model()
    assert by_model["deepseek-chat"]["calls"] == 2
    assert by_model["deepseek-chat"]["tokens"] == 4500 + 4700
    assert by_model["deepseek-reasoner"]["calls"] == 1
    assert by_model["deepseek-reasoner"]["krw"] > by_model["deepseek-chat"]["krw"]

    text = ledger.breakeven()
    assert "deepseek-chat: 2 calls" in text
    assert "deepseek-reasoner: 1 calls" in text
    assert "API budget" in text, "the budget that pauses deciding must be visible"


def test_closed_trades_and_open_lots_walk_fifo(ledger):
    """The ledger's FIFO walk feeds both the bot's P&L section and the scorer's
    closed-trade grades. Prices in the ledger are mainnet reference prices, so
    both are marked to the real market."""
    ledger.record_trade(symbol="AAAUSDT", side="BUY", quantity=10, price=100, market="CRYPTO")
    ledger.record_trade(symbol="AAAUSDT", side="SELL", quantity=4, price=110, market="CRYPTO")
    closed = ledger.closed_trades(markets={"CRYPTO"})
    assert len(closed) == 1
    assert closed[0]["pnl_quote"] == pytest.approx(40.0)  # 4 x (110 - 100)
    assert closed[0]["return_pct"] == pytest.approx(10.0)
    lots = ledger.open_lots(markets={"CRYPTO"})
    assert lots["AAAUSDT"] == [[6, 100.0]], "the trimmed remainder stays an open lot"


def test_orphan_sells_produce_no_pnl(ledger):
    """A sell with no recorded buy (seed liquidation) is not a round trip."""
    ledger.record_trade(symbol="TUTUSDT", side="SELL", quantity=18446, price=0.035, market="CRYPTO")
    assert ledger.closed_trades(markets={"CRYPTO"}) == []
    assert ledger.open_lots(markets={"CRYPTO"}) == {}


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


# -- currency correctness (the /costs mislabeling found 2026-09-01) -----------


def test_usdt_fees_convert_to_krw_in_the_day_report(ledger):
    """A Binance fee is charged in USDT; /costs reports KRW. Storing the quote
    figure under a KRW label under-reported fees by the full FX rate."""
    fee_quote = ledger.record_trade(
        symbol="BTCUSDT", side="BUY", quantity=1.0, price=10_000.0, market="CRYPTO"
    )
    day = ledger.day()
    assert day.trade_fees_krw == pytest.approx(fee_quote * 1380.0, rel=1e-3)


def test_realised_pnl_converts_from_the_venue_currency(ledger):
    ledger.record_realised(100.0, currency="USD")
    assert ledger.day().realised_pl_krw == pytest.approx(100.0 * 1380.0)
    # Default stays KRW so legacy call sites keep meaning what they said.
    ledger.record_realised(5_000)
    assert ledger.day().realised_pl_krw == pytest.approx(5_000)


def test_legacy_trade_rows_convert_only_for_configured_markets(ledger):
    """Pre-fix rows stored quote units in fee_krw with no currency field. On
    read they convert for markets with an explicit fee entry (Binance books),
    but never for removed markets (KR) whose rows were genuinely KRW."""
    import json

    ts = dt.datetime.now(dt.UTC).isoformat()
    with open(ledger.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": "trade", "market": "CRYPTO", "fee_krw": 2.0}) + "\n")
        fh.write(json.dumps({"ts": ts, "kind": "trade", "market": "KR", "fee_krw": 650.0}) + "\n")
    assert ledger.day().trade_fees_krw == pytest.approx(2.0 * 1380.0 + 650.0)


def test_flat_symbol_scoping_excludes_open_positions():
    """myTrades cash flow is only exact for a flat symbol: an open position's
    buys must read as committed cash, not as a realised loss that could trip
    the daily-loss cap."""
    import datetime as _dt

    from trading.agent.loop import TradingAgent
    from trading.brokers.state import Snapshot

    class Stub:
        _traded_symbols = frozenset({"AAAUSDT", "BBBUSDT", "CCCUSDT"})

        class state:
            @staticmethod
            def current():
                return Snapshot(
                    market="BINANCE",
                    taken_at=_dt.datetime.now(_dt.UTC),
                    positions={"rows": [{"stk_cd": "BBBUSDT", "rmnd_qty": "5"}]},
                )

    flat = TradingAgent._flat_traded_symbols(Stub())
    assert flat == ["AAAUSDT", "CCCUSDT"], "the still-held symbol must be excluded"
