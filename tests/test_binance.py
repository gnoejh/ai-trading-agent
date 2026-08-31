"""Binance client tests. No network: httpx is driven by a MockTransport."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from trading.accounting.costs import CostLedger
from trading.brokers.binance.client import BinanceClient, BinanceError
from trading.config import BinanceSecrets, load_config


@pytest.fixture
def cfg():
    c = load_config()
    c.broker.binance.use_testnet = False
    c.broker.binance.allow_orders = False
    c.broker.binance.min_call_interval_s = 0
    return c


@pytest.fixture
def secrets():
    # _env_file=None: never let the live Binance keys into a test or its output.
    return BinanceSecrets(
        _env_file=None,
        BINANCE_MAINNET_API_KEY="test-key",
        BINANCE_MAINNET_SECRET_KEY="test-secret",
    )


def make(cfg, secrets, handler, market="CRYPTO", allow_orders=False):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1786000000000})
        return handler(request)

    client = BinanceClient(
        market,
        cfg=cfg,
        secrets=secrets,
        allow_orders=allow_orders,
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
    )
    return client, calls


def ok(payload):
    return lambda _r: httpx.Response(200, json=payload)


def test_public_call_is_unsigned(cfg, secrets):
    c, calls = make(cfg, secrets, ok({"symbols": []}))
    c.call("exchange_info")
    assert "signature" not in str(calls[-1].url)
    assert "X-MBX-APIKEY" not in calls[-1].headers


def test_private_call_is_signed(cfg, secrets):
    c, calls = make(cfg, secrets, ok({"balances": []}))
    c.call("account")
    url = str(calls[-1].url)
    assert "signature=" in url and "timestamp=" in url and "recvWindow=" in url
    assert calls[-1].headers["X-MBX-APIKEY"] == "test-key"


def test_testnet_splits_data_and_trade_planes(cfg):
    """use_testnet must move ONLY signed traffic. Market data stays on mainnet:
    the testnet's bot-seeded books would feed the flow screen noise."""
    cfg.broker.binance.use_testnet = True
    tn_secrets = BinanceSecrets(
        _env_file=None,
        BINANCE_MAINNET_API_KEY="main-key",
        BINANCE_MAINNET_SECRET_KEY="main-secret",
        BINANCE_TESTNET_API_KEY="testnet-key",
        BINANCE_TESTNET_SECRET_KEY="testnet-secret",
    )
    c, calls = make(cfg, tn_secrets, ok({"balances": []}))

    c.call("ticker_24hr")
    assert calls[-1].url.host == "api.binance.com"
    assert "X-MBX-APIKEY" not in calls[-1].headers

    c.call("account")
    assert calls[-1].url.host == "testnet.binance.vision"
    assert calls[-1].headers["X-MBX-APIKEY"] == "testnet-key"
    # Clock drift is measured against the host that receives the signed
    # timestamps, i.e. the trade plane.
    time_calls = [r for r in calls if r.url.path == "/api/v3/time"]
    assert time_calls and all(r.url.host == "testnet.binance.vision" for r in time_calls)


def test_tradable_pool_is_limited_to_trade_plane_listings(cfg):
    """Found live 2026-08-31: the widened pool offered a mainnet symbol the
    testnet does not list, and the explore order died with -1121 Invalid
    symbol. The pool's contract is 'an order could execute', so the data-plane
    universe must be intersected with what the TRADE host lists."""
    from types import SimpleNamespace

    from trading.brokers.binance.universe import BinanceScreen

    cfg.broker.binance.use_testnet = True
    tn_secrets = BinanceSecrets(
        _env_file=None,
        BINANCE_MAINNET_API_KEY="main-key",
        BINANCE_MAINNET_SECRET_KEY="main-secret",
        BINANCE_TESTNET_API_KEY="testnet-key",
        BINANCE_TESTNET_SECRET_KEY="testnet-secret",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo":
            # The TRADE host (testnet) lists only AAAUSDT.
            assert request.url.host == "testnet.binance.vision"
            return httpx.Response(
                200, json={"symbols": [{"symbol": "AAAUSDT", "status": "TRADING"}]}
            )
        if request.url.path == "/api/v3/ticker/24hr":
            assert request.url.host == "api.binance.com"
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": s,
                        "lastPrice": "2.0",
                        "priceChangePercent": "1",
                        "quoteVolume": "9000000",
                    }
                    for s in ("AAAUSDT", "BBBUSDT")
                ],
            )
        return httpx.Response(200, json={"rows": []})

    client = BinanceClient(
        "CRYPTO",
        cfg=cfg,
        secrets=tn_secrets,
        allow_orders=False,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    universe = SimpleNamespace(
        symbols={"AAAUSDT", "BBBUSDT"},
        rules_for=lambda s: None,
        book_of=lambda s: "CRYPTO",
    )
    pool = BinanceScreen(client, universe, cfg).tradable_pool()
    assert [e["symbol"] for e in pool] == ["AAAUSDT"], (
        "a symbol the trade plane does not list must never reach an order"
    )


def test_bare_list_response_is_normalised(cfg, secrets):
    """Binance returns bare lists; they are normalised so callers share one shape."""
    c, _ = make(cfg, secrets, ok([{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]))
    body = c.call("ticker_24hr").body
    assert body["rows"][0]["symbol"] == "BTCUSDT"


def test_order_refused_unless_enabled(cfg, secrets):
    c, _ = make(cfg, secrets, ok({}))
    with pytest.raises(PermissionError, match="risk gate"):
        c.call("order", {"symbol": "BTCUSDT", "side": "BUY"})


def test_order_allowed_when_enabled(cfg, secrets):
    c, calls = make(cfg, secrets, ok({"orderId": 1}), allow_orders=True)
    c.call("order", {"symbol": "BTCUSDT", "side": "BUY"})
    assert calls[-1].method == "POST"


def test_api_error_is_raised_with_binance_code(cfg, secrets):
    def handler(_r):
        return httpx.Response(400, json={"code": -2010, "msg": "insufficient balance"})

    c, _ = make(cfg, secrets, handler)
    with pytest.raises(BinanceError, match="-2010"):
        c.call("account")


def test_clock_drift_is_corrected_before_signing(cfg, secrets):
    """A skewed local clock otherwise fails every signed call with -1021."""
    c, _ = make(cfg, secrets, ok({"balances": []}))
    c.call("account")
    assert c._time_offset_ms != 0


# -- per-venue economics -----------------------------------------------------


def test_book_hurdles_differ(cfg, tmp_path):
    """One shared fee model would misprice break-even on one book or the other."""
    cfg.accounting.ledger = str(tmp_path / "ledger.jsonl")
    ledger = CostLedger(cfg)
    crypto = ledger.breakeven_move_pct("CRYPTO")
    bstocks = ledger.breakeven_move_pct("BSTOCKS")
    assert crypto != bstocks
    # Same commission, wider assumed slippage on the thinner bStocks book.
    assert crypto == pytest.approx(0.001 * 2 + 0.0015 * 2)
    assert bstocks == pytest.approx(0.001 * 2 + 0.0020 * 2)


def test_no_sell_side_tax_on_binance(cfg, tmp_path):
    cfg.accounting.ledger = str(tmp_path / "ledger.jsonl")
    ledger = CostLedger(cfg)
    cr_buy = ledger.trade_fee(1_000_000, side="BUY", market="CRYPTO")
    cr_sell = ledger.trade_fee(1_000_000, side="SELL", market="CRYPTO")
    assert cr_sell == pytest.approx(cr_buy), "Binance has no sell-side tax"


def test_unknown_market_falls_back_to_default_fees(cfg, tmp_path):
    cfg.accounting.ledger = str(tmp_path / "ledger.jsonl")
    ledger = CostLedger(cfg)
    assert ledger.breakeven_move_pct("NOSUCH") == pytest.approx(
        cfg.accounting.fees.round_trip_rate()
    )


def _basis_state(cfg, secrets, trades):
    from types import SimpleNamespace

    from trading.brokers.binance.account import BinanceAccountState

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=trades)
        return httpx.Response(200, json={"rows": []})

    client, _ = make(cfg, secrets, handler)
    return BinanceAccountState(client, SimpleNamespace(), cfg)


def test_cost_basis_survives_sell_then_rebuy(cfg, secrets):
    """Found live 2026-08-31: TUTUSDT's seed balance was liquidated, then the
    explore arm re-bought it. The old newest-first walk reset on the OLDER
    sell and wiped the rebuy's basis to 0 -- the position was refused
    management and sat with no stop."""
    trades = [
        {"qty": "18446", "quoteQty": "657.78", "isBuyer": False},  # seed liquidation
        {"qty": "11671", "quoteQty": "424.20", "isBuyer": True},  # explore re-entry
    ]
    basis = _basis_state(cfg, secrets, trades).cost_basis("TUTUSDT")
    assert basis == pytest.approx(424.20 / 11671)


def test_cost_basis_survives_a_partial_scale_out(cfg, secrets):
    """A trailing sell is a trim, not a closed round trip: the remaining
    position keeps its average cost."""
    trades = [
        {"qty": "100", "quoteQty": "1000", "isBuyer": True},  # avg 10
        {"qty": "30", "quoteQty": "390", "isBuyer": False},  # scale out
    ]
    basis = _basis_state(cfg, secrets, trades).cost_basis("XUSDT")
    assert basis == pytest.approx(10.0)


def test_binance_holdings_use_cost_basis_for_avg_price(cfg):
    from trading.brokers.adapters import BinanceAdapter
    from trading.brokers.state import Snapshot

    adapter = BinanceAdapter("CRYPTO", cfg)
    snapshot = Snapshot(
        market="CRYPTO",
        taken_at=dt.datetime.now(dt.UTC),
        positions={"rows": [{"stk_cd": "BTCUSDT", "rmnd_qty": 1.0, "cur_prc": 100.0}]},
        cash={"entr": 5000},
        open_orders={},
        evaluation={},
    )
    adapter._state.cost_basis = lambda symbol: 95.0
    holdings = adapter.holdings(snapshot)
    assert holdings["BTCUSDT"]["avg_price"] == pytest.approx(95.0)
    assert holdings["BTCUSDT"]["cost_basis"] == pytest.approx(95.0)


def test_binance_cost_basis_cached_until_quantity_changes(cfg):
    """One myTrades call per (symbol, quantity), not per cycle.

    The testnet seeds ~480 balances this system never bought; re-discovering
    "no fills" for each cost one signed call per symbol EVERY cycle. A fill or
    deposit changes the balance, which is what invalidates the cache.
    """
    from trading.brokers.adapters import BinanceAdapter
    from trading.brokers.state import Snapshot

    adapter = BinanceAdapter("CRYPTO", cfg)
    calls: list[str] = []

    def counting_basis(symbol):
        calls.append(symbol)
        return 95.0

    adapter._state.cost_basis = counting_basis

    def snap(qty):
        return Snapshot(
            market="CRYPTO",
            taken_at=dt.datetime.now(dt.UTC),
            positions={"rows": [{"stk_cd": "BTCUSDT", "rmnd_qty": qty}]},
            cash={"entr": 5000},
            open_orders={},
            evaluation={},
        )

    adapter.holdings(snap(1.0))
    adapter.holdings(snap(1.0))
    assert calls == ["BTCUSDT"], "unchanged quantity must not re-query myTrades"

    adapter.holdings(snap(2.0))
    assert calls == ["BTCUSDT", "BTCUSDT"], "a changed quantity must force a fresh read"


# -- execution ---------------------------------------------------------------


def _rules(step="0.001", min_notional="5"):
    from decimal import Decimal

    from trading.brokers.binance.symbols import SymbolRules

    return SymbolRules(
        symbol="NVDABUSDT",
        step_size=Decimal(step),
        min_qty=Decimal(step),
        tick_size=Decimal("0.01"),
        min_notional=Decimal(min_notional),
    )


class _Universe:
    symbols = frozenset({"NVDABUSDT"})

    def rules_for(self, symbol):
        return _rules()

    def book_of(self, symbol):
        return "BSTOCKS"


class _Gate:
    def __init__(self):
        self.sent = 0

    def record_sent(self):
        self.sent += 1


def _verdict(side, qty, price=224.9, approved=True):
    from trading.risk.gate import Side, TradeIntent, Verdict

    return Verdict(
        approved,
        TradeIntent(
            market="CRYPTO",
            side=Side(side),
            symbol="NVDABUSDT",
            quantity=qty,
            reference_price=price,
        ),
    )


def _executor(cfg, secrets, handler, dry_run=False):
    from trading.brokers.binance.account import BinanceExecutor

    c, calls = make(cfg, secrets, handler, allow_orders=True)
    return BinanceExecutor(c, _Gate(), _Universe(), cfg, dry_run=dry_run), calls


def test_quantity_is_not_sent_in_scientific_notation(cfg, secrets):
    """str(1e-05) renders as '1e-05', which Binance rejects outright."""
    assert str(0.00001) == "1e-05", "the trap this guards against"
    ex, _ = _executor(cfg, secrets, ok({"orderId": 1}), dry_run=True)
    params = ex._params(_verdict("BUY", 0.00001).intent, _rules(step="0.00001"))
    assert "e" not in params["quantity"].lower()
    assert params["quantity"].startswith("0.00001")


def test_sell_cancels_resting_orders_that_reserve_the_asset(cfg, secrets):
    """A holding under a resting stop shows free=0; the sell would otherwise fail."""
    seen = {"cancelled": 0, "ordered": 0}

    def handler(request):
        path = request.url.path
        if path == "/api/v3/openOrders":
            return httpx.Response(
                200,
                json=[{"orderId": 99, "side": "SELL", "origQty": "0.391", "executedQty": "0"}],
            )
        if path == "/api/v3/order" and request.method == "DELETE":
            seen["cancelled"] += 1
            return httpx.Response(200, json={"orderId": 99, "status": "CANCELED"})
        seen["ordered"] += 1
        return httpx.Response(200, json={"orderId": 100})

    ex, _ = _executor(cfg, secrets, handler)
    ex.execute(_verdict("SELL", 0.391))
    assert seen["cancelled"] == 1, "must free the reserved quantity before selling"
    assert seen["ordered"] == 1


def test_buy_does_not_cancel_anything(cfg, secrets):
    seen = {"cancelled": 0}

    def handler(request):
        if request.method == "DELETE":
            seen["cancelled"] += 1
        return httpx.Response(200, json={"orderId": 1})

    ex, _ = _executor(cfg, secrets, handler)
    ex.execute(_verdict("BUY", 1.0))
    assert seen["cancelled"] == 0


def test_rejected_verdict_never_reaches_the_exchange(cfg, secrets):
    from trading.brokers.state import OrderRejected

    ex, calls = _executor(cfg, secrets, ok({"orderId": 1}))
    with pytest.raises(OrderRejected):
        ex.execute(_verdict("BUY", 1.0, approved=False))
    assert not [c for c in calls if c.url.path == "/api/v3/order"]


def test_order_below_min_notional_is_caught_before_the_round_trip(cfg, secrets):
    ex, calls = _executor(cfg, secrets, ok({"orderId": 1}))
    with pytest.raises(ValueError, match="minNotional"):
        ex.execute(_verdict("BUY", 0.001, price=1.0))  # $0.001 notional
    assert not [c for c in calls if c.url.path == "/api/v3/order"]
