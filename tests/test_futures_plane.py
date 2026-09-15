"""The futures data plane: positioning reads, never trades.

`host: futures` on an endpoint routes to `futures_data_url` regardless of
`use_testnet`, and a signed futures endpoint is refused outright -- this
system holds no futures account and must never look as if it might.
"""

from __future__ import annotations

import httpx
import pytest

from trading.brokers.binance.client import BinanceClient
from trading.config import BinanceEndpoint, load_config


def _client(cfg, handler):
    return BinanceClient(
        "CRYPTO", cfg=cfg, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_a_futures_endpoint_hits_the_futures_host_even_on_testnet():
    cfg = load_config()
    cfg.broker.binance.use_testnet = True
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, json=[{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1}]
        )

    page = _client(cfg, handler).call("funding_history", {"symbol": "BTCUSDT"})
    assert seen[0].startswith(cfg.broker.binance.futures_data_url + "/fapi/v1/fundingRate")
    assert page.body["rows"][0]["fundingRate"] == "0.0001"


def test_a_spot_endpoint_is_untouched():
    cfg = load_config()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    _client(cfg, handler).call("klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1})
    assert seen[0].startswith("https://api.binance.com/api/v3/klines")


def test_a_signed_futures_endpoint_is_refused():
    cfg = load_config()
    cfg.broker.binance.endpoints["fut_order"] = BinanceEndpoint(
        path="/fapi/v1/order", method="POST", signed=True, order=True, host="futures"
    )

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a signed futures call was sent")

    with pytest.raises(ValueError, match="no signed futures"):
        _client(cfg, handler).call("fut_order", {"symbol": "BTCUSDT"})


def test_host_defaults_to_spot():
    assert BinanceEndpoint(path="/x").host == "spot"
