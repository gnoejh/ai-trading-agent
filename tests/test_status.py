"""Status rendering tests. Uses spec-derived labels, so no network."""

from __future__ import annotations

import datetime as dt

import pytest

from trading.brokers.kiwoom.account import Snapshot
from trading.config import load_config
from trading.notify.status import _num, format_status
from trading.rag.spec_store import SpecStore


@pytest.fixture(scope="session")
def store():
    return SpecStore.load(load_config().specs.index)


@pytest.fixture(scope="session")
def endpoints():
    return load_config().broker.kiwoom.market("KR").state


@pytest.fixture
def snapshot():
    return Snapshot(
        market="KR",
        taken_at=dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.UTC),
        cash={"return_code": 0, "entr": "0000012345678", "profa_ch": "0000000500000"},
        positions={
            "return_code": 0,
            "tot_evlt_amt": "0000098765432",
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "005930",
                    "stk_nm": "삼성전자",
                    "rmnd_qty": "0000000100",
                    "evltv_prft": "-0000123456",
                },
                {
                    "stk_cd": "000660",
                    "stk_nm": "SK하이닉스",
                    "rmnd_qty": "0000000050",
                    "evltv_prft": "0000234567",
                },
            ],
        },
        open_orders={
            "return_code": 0,
            "oso": [{"stk_nm": "삼성전자", "ord_qty": "10", "oso_qty": "4"}],
        },
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0000012345678", "12,345,678"),
        ("-0000123456", "-123,456"),
        ("+0000000100", "100"),
        ("0000000000", "0"),
        ("", "-"),
        (None, "-"),
        ("KRX", "KRX"),  # non-numeric passes through
    ],
)
def test_num_formatting(raw, expected):
    assert _num(raw) == expected


def test_status_includes_positions_and_counts(snapshot, store, endpoints):
    text = format_status(snapshot, store, endpoints)
    assert "삼성전자" in text and "SK하이닉스" in text
    assert "*보유종목 / Positions* (2)" in text
    assert "*미체결 / Open orders* (1)" in text
    assert "x100" in text  # leading zeros stripped
    assert "-123,456" in text  # loss keeps its sign


def test_labels_come_from_the_spec(snapshot, store, endpoints):
    """`entr` should render with its Korean label, not the raw key."""
    text = format_status(snapshot, store, endpoints)
    label = {
        f.element: f.korean_name for f in store.get(endpoints.cash.api_id).body_fields("response")
    }
    assert label.get("entr")
    assert f"{label['entr']}: 12,345,678" in text


def test_empty_account_renders(store, endpoints):
    empty = Snapshot(
        market="KR",
        taken_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
        cash={"return_code": 0},
        positions={"return_code": 0},
        open_orders={"return_code": 0},
    )
    text = format_status(empty, store, endpoints)
    assert "*보유종목 / Positions* (0)" in text
    assert text.count("none") == 2


def test_position_list_is_truncated(snapshot, store, endpoints):
    snapshot.positions["acnt_evlt_remn_indv_tot"] = [
        {"stk_nm": f"종목{i}", "rmnd_qty": "1"} for i in range(20)
    ]
    text = format_status(snapshot, store, endpoints, max_positions=5)
    assert "… and 15 more" in text
