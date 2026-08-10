"""Symbol-filter tests.

Binance rejects orders that violate stepSize / minQty / minNotional. These use the
real filter values observed live on 2026-08-10.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading.brokers.binance.symbols import SymbolRules, parse_symbol
from trading.config import load_config
from trading.risk.sizing import PositionSizer


def rules(**kw) -> SymbolRules:
    base = {
        "symbol": "BTCUSDT",
        "step_size": Decimal("0.00001"),
        "min_qty": Decimal("0.00001"),
        "tick_size": Decimal("0.01"),
        "min_notional": Decimal(5),
    }
    return SymbolRules(**{**base, **kw})


def test_parses_live_filter_shape():
    r = parse_symbol(
        {
            "symbol": "NVDABUSDT",
            "baseAsset": "NVDAB",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            ],
            "permissionSets": [["SPOT", "TRD_GRP_261"]],
        }
    )
    assert r.step_size == Decimal("0.001")
    assert r.min_notional == Decimal(5)
    assert "TRD_GRP_261" in r.permissions


def test_min_notional_accepts_the_legacy_filter_name():
    """Binance renamed MIN_NOTIONAL to NOTIONAL; both still appear."""
    r = parse_symbol(
        {
            "symbol": "X",
            "filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10"}],
        }
    )
    assert r.min_notional == Decimal(10)


@pytest.mark.parametrize(
    "raw,step,expected",
    [
        (0.123456789, "0.00001", "0.12345"),  # BTCUSDT
        (1234.9, "1", "1234"),  # SHIBUSDT — whole units
        (0.3919, "0.001", "0.391"),  # NVDABUSDT
    ],
)
def test_quantity_rounds_down_to_step(raw, step, expected):
    r = rules(step_size=Decimal(step), min_qty=Decimal(step))
    assert r.quantize_qty(raw) == Decimal(expected)


def test_rounding_never_goes_up():
    """Rounding up can exceed the balance and be rejected for insufficient funds."""
    r = rules()
    assert r.quantize_qty(0.999999999) <= Decimal("0.999999999")


def test_rejects_below_min_notional():
    r = rules()
    # 0.00002 BTC at $100 = $0.002, far below the $5 floor
    assert r.rejects(Decimal("0.00002"), Decimal(100))
    assert "minNotional" in r.rejects(Decimal("0.00002"), Decimal(100))


def test_rejects_zero_after_quantisation():
    r = rules(step_size=Decimal(1), min_qty=Decimal(1))
    assert "zero" in r.rejects(r.quantize_qty(0.4), Decimal(100))


def test_accepts_a_valid_order():
    r = rules()
    assert r.is_valid(Decimal("0.001"), Decimal(60000))


def test_price_quantised_to_tick():
    r = rules(tick_size=Decimal("0.01"))
    assert r.quantize_price(123.4567) == Decimal("123.45")


# -- sizer integration -------------------------------------------------------


@pytest.fixture
def sizer():
    cfg = load_config()
    cfg.sizing.mode = "full_balance"
    cfg.sizing.reserve_pct = 0.0
    return PositionSizer(cfg)


def test_sizer_quantises_to_the_symbol_step(sizer):
    cash = {"ord_alow_amt": "1000"}
    qty = sizer.quantity_for(60_000.0, cash, rules())
    assert Decimal(str(qty)) % Decimal("0.00001") == 0
    assert qty * 60_000 <= 1000


def test_sizer_returns_zero_when_budget_cannot_clear_min_notional(sizer):
    """$1 cannot buy a $5-minimum order; emitting one would just be rejected."""
    assert sizer.quantity_for(60_000.0, {"ord_alow_amt": "1"}, rules()) == 0


def test_whole_share_path_unchanged_without_rules(sizer):
    """KRX still sizes in whole shares when no venue rules are supplied."""
    qty = sizer.quantity_for(70_000.0, {"ord_alow_amt": "1845784"})
    assert qty == int(qty) and qty == 26
