"""Client guardrail tests. No network: httpx is driven by a MockTransport."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from trading.brokers.kiwoom.account import AccountState, StaleStateError
from trading.brokers.kiwoom.client import KiwoomClient
from trading.config import KiwoomSecrets, load_config
from trading.rag.spec_parser import Market
from trading.rag.spec_store import SpecStore


@pytest.fixture
def cfg():
    # Pin the trading-critical switches rather than inheriting config.yaml: these
    # tests must behave identically whether or not the live config is set to trade.
    c = load_config()
    c.broker.kiwoom.use_testnet = True
    c.broker.kiwoom.allow_orders = False
    return c


@pytest.fixture(scope="session")
def store():
    # Session-scoped and independent of `cfg`: parsing the index is slow and the
    # spec data is immutable.
    return SpecStore.load(load_config().specs.index)


@pytest.fixture
def secrets():
    # _env_file=None matters: without it pydantic-settings loads the real .env and
    # live mainnet keys end up in pytest failure output.
    return KiwoomSecrets(
        _env_file=None,
        KIWOOM_TESTNET_APP_KEY="test-key",
        KIWOOM_TESTNET_SECRET_KEY="test-secret",
    )


def make_client(cfg, store, secrets, *, market=Market.KR, allow_orders=False, handler=None):
    calls: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth2/token":
            expires = (
                dt.datetime.now(ZoneInfo(cfg.broker.kiwoom.timezone)) + dt.timedelta(hours=6)
            ).strftime("%Y%m%d%H%M%S")
            return httpx.Response(
                200, json={"token": "tok", "token_type": "Bearer", "expires_dt": expires}
            )
        return httpx.Response(200, json={"return_code": 0, "return_msg": "ok"})

    transport = httpx.MockTransport(handler or default_handler)
    client = KiwoomClient(
        market,
        store=store,
        cfg=cfg,
        secrets=secrets,
        allow_orders=allow_orders,
        client=httpx.Client(transport=transport),
    )
    return client, calls


def test_order_endpoints_refused_by_default(cfg, store, secrets):
    client, _ = make_client(cfg, store, secrets)
    with pytest.raises(PermissionError, match="risk gate"):
        client.call(
            "kt10000", {"dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"}
        )


def test_order_allowed_when_explicitly_enabled(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets, allow_orders=True)
    client.call(
        "kt10000", {"dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"}
    )
    assert calls[-1].url.path == "/api/dostk/ordr"
    assert calls[-1].headers["api-id"] == "kt10000"


def test_cross_market_id_is_rejected(cfg, store, secrets):
    client, _ = make_client(cfg, store, secrets, market=Market.KR)
    with pytest.raises(ValueError, match="belongs to market US"):
        client.call("ust21070")


def test_missing_required_field_rejected_before_network(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    with pytest.raises(ValueError, match="missing required field"):
        client.call("ka10001", {})
    assert not [c for c in calls if c.url.path != "/oauth2/token"]


def test_unknown_field_rejected(cfg, store, secrets):
    client, _ = make_client(cfg, store, secrets)
    with pytest.raises(ValueError, match="unknown field"):
        client.call("ka10001", {"stk_cd": "005930", "bogus": 1})


def test_oversized_field_rejected(cfg, store, secrets):
    client, _ = make_client(cfg, store, secrets)
    with pytest.raises(ValueError, match="spec max"):
        client.call("ka10001", {"stk_cd": "0" * 40})


def test_websocket_id_rejected_on_rest_client(cfg, store, secrets):
    client, _ = make_client(cfg, store, secrets)
    with pytest.raises(ValueError, match="realtime subscription"):
        client.call("0B")


def test_testnet_domain_used(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    client.call("ka10001", {"stk_cd": "005930"})
    assert all(c.url.host == "mockapi.kiwoom.com" for c in calls)


def test_token_reused_across_calls(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    client.call("ka10001", {"stk_cd": "005930"})
    client.call("ka10001", {"stk_cd": "000660"})
    assert sum(1 for c in calls if c.url.path == "/oauth2/token") == 1


def test_error_return_code_raises(cfg, store, secrets):
    def handler(request):
        if request.url.path == "/oauth2/token":
            expires = (
                dt.datetime.now(ZoneInfo(cfg.broker.kiwoom.timezone)) + dt.timedelta(hours=6)
            ).strftime("%Y%m%d%H%M%S")
            return httpx.Response(200, json={"token": "t", "expires_dt": expires})
        return httpx.Response(200, json={"return_code": 3, "return_msg": "잘못된 종목코드"})

    client, _ = make_client(cfg, store, secrets, handler=handler)
    with pytest.raises(Exception, match=r"\[3\]"):
        client.call("ka10001", {"stk_cd": "005930"})


def test_pagination_follows_next_key(cfg, store, secrets):
    pages = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth2/token":
            expires = (
                dt.datetime.now(ZoneInfo(cfg.broker.kiwoom.timezone)) + dt.timedelta(hours=6)
            ).strftime("%Y%m%d%H%M%S")
            return httpx.Response(200, json={"token": "t", "expires_dt": expires})
        pages["n"] += 1
        if pages["n"] < 3:
            return httpx.Response(
                200, json={"return_code": 0}, headers={"cont-yn": "Y", "next-key": f"k{pages['n']}"}
            )
        return httpx.Response(200, json={"return_code": 0}, headers={"cont-yn": "N"})

    client, _ = make_client(cfg, store, secrets, handler=handler)
    assert len(list(client.paginate("ka10001", {"stk_cd": "005930"}))) == 3


# -- broker records are the single source of truth ---------------------------


def test_account_reads_go_to_configured_state_endpoints(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    state = AccountState(client)
    state.positions()
    endpoint = cfg.broker.kiwoom.market("KR").state.positions
    assert calls[-1].headers["api-id"] == endpoint.api_id
    assert json.loads(calls[-1].content) == endpoint.params


def test_snapshot_is_refreshed_when_stale(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    state = AccountState(client)
    first = state.refresh()
    reads = len(calls)
    state.current()  # fresh -> no new reads
    assert len(calls) == reads
    first.taken_at -= dt.timedelta(seconds=cfg.state.max_staleness_s + 1)
    state.current()  # stale -> re-reads from the broker
    assert len(calls) > reads


def test_order_path_forces_reconciliation(cfg, store, secrets):
    client, calls = make_client(cfg, store, secrets)
    state = AccountState(client)
    state.refresh()
    reads = len(calls)
    state.assert_reconciled()
    assert len(calls) > reads, "assert_reconciled must re-read the broker, not trust the cache"


def test_stale_snapshot_raises_when_reconcile_disabled(cfg, store, secrets):
    cfg2 = load_config()
    cfg2.broker.kiwoom.use_testnet = True
    cfg2.state.reconcile_before_order = False
    client, _ = make_client(cfg2, store, secrets)
    state = AccountState(client)
    snap = state.refresh()
    snap.taken_at -= dt.timedelta(seconds=cfg2.state.max_staleness_s + 10)
    with pytest.raises(StaleStateError, match="old"):
        state.assert_reconciled()
