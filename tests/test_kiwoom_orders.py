"""Kiwoom order body: the wire format the broker actually accepts.

On 2026-09-03 every KR paper stop-loss sell was rejected with 1517 "ord_qty
정수만 입력가능": exit quantities are floats since the float-exit fix and the
body sent `str(41.0)`. Buys, sized as ints, went through -- twelve positions
with no working stop. These tests pin the seam, not the helper alone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading.brokers.kiwoom.orders import TRADE_TYPE_MARKET, OrderExecutor
from trading.config import load_config
from trading.risk.gate import Side, TradeIntent


class _Spec:
    url = "/api/dostk/ordr"

    def required_body(self):
        return ["dmst_stex_tp", "stk_cd", "ord_qty", "trde_tp"]


@pytest.fixture
def executor():
    cfg = load_config()
    cfg.broker.kiwoom.allow_orders = False
    client = SimpleNamespace(market="KR", store=SimpleNamespace(get=lambda api_id: _Spec()))
    return OrderExecutor(client, gate=None, cfg=cfg, dry_run=True)


def _exit(quantity):
    return TradeIntent(
        market="KR",
        side=Side.SELL,
        symbol="039030",
        quantity=quantity,
        reference_price=420000.0,
        reason="stop_loss",
    )


def test_float_exit_quantity_is_sent_as_an_integer_string(executor):
    body = executor._body(_exit(41.0))
    assert body["ord_qty"] == "41"
    assert body["trde_tp"] == TRADE_TYPE_MARKET
    assert body["dmst_stex_tp"] == "KRX"


def test_int_quantity_unchanged(executor):
    assert executor._body(_exit(2154))["ord_qty"] == "2154"


@pytest.mark.parametrize("qty", [41.5, 0.0, 0.4])
def test_fractional_or_empty_quantity_is_refused_not_truncated(executor, qty):
    with pytest.raises(ValueError, match="whole shares"):
        executor._body(_exit(qty))
