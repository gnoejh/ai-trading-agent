"""Parser tests against the real workbook index.

These assert the vendor layout assumptions the parser depends on, so a workbook
revision that changes them fails here rather than at trade time.
"""

from __future__ import annotations

import pytest

from trading.config import load_config
from trading.rag.spec_parser import Market, market_of
from trading.rag.spec_store import SpecStore


@pytest.fixture(scope="session")
def store() -> SpecStore:
    return SpecStore.load(load_config().specs.index)


def test_every_catalog_entry_has_a_spec(store):
    missing = [
        e.api_id for e in store.catalog if e.api_id not in store.specs and e.api_id != "공통"
    ]
    assert not missing


def test_error_code_sheet_is_reachable_by_catalog_id(store):
    assert store.get("공통") is not None


def test_query_spec_fields(store):
    spec = store.get("ka10001")  # 주식기본정보요청
    assert spec.method == "POST"
    assert spec.url == "/api/dostk/stkinfo"
    assert spec.market is Market.KR
    assert spec.required_body() == ["stk_cd"]
    assert "stk_nm" in {f.element for f in spec.body_fields("response")}


def test_nested_list_fields_get_a_parent(store):
    spec = store.get("ka10003")  # 체결정보요청, response is a list under cntr_infr
    nested = [f for f in spec.body_fields("response") if f.parent]
    assert nested, "expected `- ` prefixed rows to be parsed as children"
    assert {f.parent for f in nested} == {"cntr_infr"}


def test_order_spec_required_fields(store):
    spec = store.get("kt10000")  # 주식 매수주문
    assert set(spec.required_body()) == {"dmst_stex_tp", "stk_cd", "ord_qty", "trde_tp"}


def test_realtime_sheets_are_websocket(store):
    spec = store.get("0B")  # 주식체결
    assert spec.is_websocket
    assert spec.prod_domain.startswith("wss://")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/api/dostk/ordr", Market.KR),
        ("/api/us/acnt", Market.US),
        ("/oauth2/token", Market.AUTH),
        ("", Market.COMMON),
    ],
)
def test_market_of(url, expected):
    assert market_of(url) is expected


def test_markets_do_not_overlap(store):
    kr = {e.api_id for e in store.entries(Market.KR)}
    us = {e.api_id for e in store.entries(Market.US)}
    assert not (kr & us)
    assert kr and us
