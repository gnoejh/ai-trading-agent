"""Per-sleeve daily P&L tests.

The defect this module exists to remove is POOLING: on 2026-09-13 the gate's
profit criteria read green on money the random arm earned. So these pin the
seam that keeps arms apart, not the arithmetic of one sum — attribution by
journal, the legacy category, and the standing refusal to add two currencies
together.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from trading.accounting.costs import CostLedger
from trading.agent.pnl import LEGACY, MODEL, RANDOM, attribute, entry_events, evaluate, render
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.score.trade_since = ""
    c.score.trade_markets = ["CRYPTO", "KR"]
    c.score.venues = ["BINANCE"]
    c.promotion.since = ""
    c.pnl.match_tolerance_s = 900.0
    c.pnl.days = 0
    return c


def _journal(cfg, rows):
    with open(cfg.journal_for("BINANCE"), "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)


def _order(symbol, ts):
    return {"ts": ts, "kind": "order", "intent": {"symbol": symbol, "side": "BUY"}}


def _explore(symbol, ts, sent=True):
    return {"ts": ts, "kind": "explore", "entry": {"symbol": symbol}, "sent": sent}


def _trip(symbol, entry_ts, *, market="CRYPTO", entry=10.0, exit_price=11.0, qty=1.0):
    return {
        "symbol": symbol,
        "market": market,
        "quantity": qty,
        "entry_price": entry,
        "exit_price": exit_price,
        "pnl_quote": qty * (exit_price - entry),
        "return_pct": (exit_price / entry - 1) * 100,
        "entry_ts": entry_ts,
        "exit_ts": entry_ts,
    }


def test_entry_events_read_both_arms(cfg):
    _journal(
        cfg,
        [
            _order("AAAUSDT", "2026-09-10T00:00:00+00:00"),
            _explore("BBBUSDT", "2026-09-10T01:00:00+00:00"),
            _explore("CCCUSDT", "2026-09-10T02:00:00+00:00", sent=False),
            {"ts": "2026-09-10T03:00:00+00:00", "kind": "decision"},
        ],
    )
    events = entry_events(cfg)
    assert events["AAAUSDT"][0][1] == MODEL
    assert events["BBBUSDT"][0][1] == RANDOM
    # An explore row that never went to the broker opened no position.
    assert "CCCUSDT" not in events


def test_trip_is_credited_to_the_arm_that_opened_it(cfg):
    _journal(
        cfg,
        [
            _order("AAAUSDT", "2026-09-10T00:00:00+00:00"),
            _explore("BBBUSDT", "2026-09-10T00:00:00+00:00"),
        ],
    )
    trips = attribute(
        [
            _trip("AAAUSDT", "2026-09-10T00:01:00+00:00"),
            _trip("BBBUSDT", "2026-09-10T00:01:00+00:00"),
        ],
        entry_events(cfg),
        tolerance_s=cfg.pnl.match_tolerance_s,
    )
    assert [t["arm"] for t in trips] == [MODEL, RANDOM]


def test_unmatched_trip_is_legacy_not_silently_credited(cfg):
    """The failure mode must be under-claiming.

    A trip opened before the journals existed (the fill sprint's unwind) is
    real money, but crediting it to either arm would be an invention — and
    crediting it to the model is exactly the error the module exists to stop.
    """
    _journal(cfg, [_order("AAAUSDT", "2026-09-10T00:00:00+00:00")])
    events = entry_events(cfg)
    # Same symbol, but hours outside the matching window.
    far = attribute([_trip("AAAUSDT", "2026-09-10T06:00:00+00:00")], events, tolerance_s=900.0)
    assert far[0]["arm"] == LEGACY
    # Unknown symbol entirely.
    unknown = attribute([_trip("ZZZUSDT", "2026-09-10T00:01:00+00:00")], events, tolerance_s=900.0)
    assert unknown[0]["arm"] == LEGACY


def test_one_order_fathers_every_fifo_slice(cfg):
    """FIFO splits one buy across several sells; events are not consumed.

    A partial exit followed by the rest is two closed trips from one journalled
    order. Consuming the event on the first match would orphan the second and
    quietly move that money into `legacy`.
    """
    _journal(cfg, [_order("AAAUSDT", "2026-09-10T00:00:00+00:00")])
    trips = attribute(
        [
            _trip("AAAUSDT", "2026-09-10T00:00:30+00:00", qty=0.4),
            _trip("AAAUSDT", "2026-09-10T00:00:30+00:00", qty=0.6),
        ],
        entry_events(cfg),
        tolerance_s=cfg.pnl.match_tolerance_s,
    )
    assert [t["arm"] for t in trips] == [MODEL, MODEL]


def test_nearest_entry_wins_when_a_symbol_is_bought_twice(cfg):
    _journal(
        cfg,
        [
            _explore("AAAUSDT", "2026-09-10T00:00:00+00:00"),
            _order("AAAUSDT", "2026-09-10T04:00:00+00:00"),
        ],
    )
    events = entry_events(cfg)
    early = attribute([_trip("AAAUSDT", "2026-09-10T00:02:00+00:00")], events, tolerance_s=900.0)
    late = attribute([_trip("AAAUSDT", "2026-09-10T04:02:00+00:00")], events, tolerance_s=900.0)
    assert early[0]["arm"] == RANDOM
    assert late[0]["arm"] == MODEL


def test_sleeves_are_never_pooled(cfg):
    """The report must keep arms, books and currencies apart.

    This is the 2026-09-13 reading in miniature: a losing model sleeve and a
    winning random sleeve whose sum would read as success.
    """
    ledger = CostLedger(cfg)
    ledger.record_trade(symbol="AAAUSDT", side="BUY", quantity=1, price=100.0, market="CRYPTO")
    ledger.record_trade(symbol="AAAUSDT", side="SELL", quantity=1, price=90.0, market="CRYPTO")
    ledger.record_trade(symbol="BBBUSDT", side="BUY", quantity=1, price=100.0, market="CRYPTO")
    ledger.record_trade(symbol="BBBUSDT", side="SELL", quantity=1, price=130.0, market="CRYPTO")

    now = dt.datetime.now(dt.UTC).isoformat()
    _journal(cfg, [_order("AAAUSDT", now), _explore("BBBUSDT", now)])

    result = evaluate(cfg)
    model = result["sleeves"]["CRYPTO·model"]
    random_arm = result["sleeves"]["CRYPTO·random"]
    assert model["total"]["n"] == 1 and model["total"]["net"] < 0
    assert random_arm["total"]["n"] == 1 and random_arm["total"]["net"] > 0
    # No key anywhere is a pooled total: the loser cannot hide inside a sum.
    assert set(result["sleeves"]) == {"CRYPTO·model", "CRYPTO·random"}
    assert "total" not in result


def test_each_sleeve_carries_its_own_currency(cfg):
    """Two books, two quote currencies — adding them yields a number in neither.

    Same class as the 2026-09-01 `/costs` defect, where USDT figures sat under
    KRW labels.
    """
    cfg.accounting.market_fees["KR"].currency = "KRW"
    cfg.accounting.market_fees["CRYPTO"].currency = "USD"
    ledger = CostLedger(cfg)
    ledger.record_trade(symbol="AAAUSDT", side="BUY", quantity=1, price=100.0, market="CRYPTO")
    ledger.record_trade(symbol="AAAUSDT", side="SELL", quantity=1, price=110.0, market="CRYPTO")
    ledger.record_trade(symbol="005930", side="BUY", quantity=1, price=70000.0, market="KR")
    ledger.record_trade(symbol="005930", side="SELL", quantity=1, price=71000.0, market="KR")
    _journal(cfg, [])

    result = evaluate(cfg)
    assert result["sleeves"]["CRYPTO·legacy"]["currency"] == "USD"
    assert result["sleeves"]["KR·legacy"]["currency"] == "KRW"


def _raw_trade(cfg, *, ts, symbol, side, quantity, price, market="CRYPTO"):
    """Append a ledger trade at a CHOSEN time — `record_trade` always stamps now."""
    with open(cfg.accounting.ledger, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": ts,
                    "kind": "trade",
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "market": market,
                    "notional": quantity * price,
                    "fee_krw": 0.0,
                }
            )
            + "\n"
        )


def test_daily_buckets_split_by_the_day_the_trip_closed(cfg):
    """The owner asked for DAILY P&L: one epoch average hides the shape.

    Realised money belongs to the day it landed, so the bucket key is the exit
    day, not the entry day — both trips below are entered the same morning and
    must land on different days.
    """
    _journal(cfg, [_order("AAAUSDT", "2026-09-10T00:00:00+00:00")])
    _raw_trade(
        cfg, ts="2026-09-10T00:00:10+00:00", symbol="AAAUSDT", side="BUY", quantity=2, price=100.0
    )
    _raw_trade(
        cfg, ts="2026-09-10T10:00:00+00:00", symbol="AAAUSDT", side="SELL", quantity=1, price=120.0
    )
    _raw_trade(
        cfg, ts="2026-09-11T10:00:00+00:00", symbol="AAAUSDT", side="SELL", quantity=1, price=80.0
    )

    sleeve = evaluate(cfg)["sleeves"]["CRYPTO·model"]
    assert list(sleeve["days"]) == ["2026-09-10", "2026-09-11"]
    assert sleeve["days"]["2026-09-10"]["net"] > 0
    assert sleeve["days"]["2026-09-11"]["net"] < 0
    # Both slices of the one buy are the model's; neither leaked to `legacy`.
    assert sleeve["total"]["n"] == 2


def test_a_days_window_never_changes_the_total(cfg):
    """`pnl.days` trims what is PRINTED, never what is measured."""
    _journal(cfg, [])
    for day, price in (("10", 120.0), ("11", 130.0), ("12", 90.0)):
        _raw_trade(
            cfg,
            ts=f"2026-09-{day}T00:00:10+00:00",
            symbol="AAAUSDT",
            side="BUY",
            quantity=1,
            price=100.0,
        )
        _raw_trade(
            cfg,
            ts=f"2026-09-{day}T10:00:00+00:00",
            symbol="AAAUSDT",
            side="SELL",
            quantity=1,
            price=price,
        )
    full = render(cfg, days=0)
    trimmed = render(cfg, days=1)
    assert trimmed.count("2026-09-") < full.count("2026-09-")
    total = evaluate(cfg)["sleeves"]["CRYPTO·legacy"]["total"]["n"]
    assert total == 3
    assert f"n={total:>3}" in trimmed


def test_render_survives_an_empty_epoch(cfg):
    _journal(cfg, [])
    out = render(cfg)
    assert "no closed trips" in out
