"""The live screen attaches the same features the replay validated.

One definition (`features.py`) is only worth something if the live path calls
it on the same kind of bars and survives a missing symbol without biasing the
menu. Pinned with a stub client that serves hourly bars.
"""

from __future__ import annotations

from types import SimpleNamespace

from trading.agent.features import PATH_KEYS, RS_KEYS
from trading.brokers.binance.universe import BinanceScreen
from trading.config import load_config


def _bars(n, start=100.0, step=1.0):
    out = []
    for i in range(n):
        c = start + i * step
        out.append([i * 3_600_000, c, c * 1.01, c * 0.99, c, 1.0, 0, 100.0, 1, 0.5, 55.0])
    return out


class _Client:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def call(self, name, params):
        assert name == "klines" and params["interval"] == "1h"
        self.calls.append(params["symbol"])
        if params["symbol"] in self.fail:
            raise RuntimeError("boom")
        step = 2.0 if params["symbol"] == "STRONG" else 1.0
        return SimpleNamespace(body={"rows": _bars(params["limit"], step=step)})


def _screen(client):
    cfg = load_config()
    cfg.agent.screen.path_features = True
    cfg.agent.screen.benchmark_symbol = "BTCUSDT"
    return BinanceScreen(client, SimpleNamespace(), cfg)


def test_features_and_regime_are_attached_from_hourly_bars():
    client = _Client()
    selected = [{"symbol": "STRONG", "book": "CRYPTO"}, {"symbol": "WEAK", "book": "CRYPTO"}]
    _screen(client)._attach_path_features(selected)
    strong, weak = selected
    assert set(PATH_KEYS + RS_KEYS) <= set(strong)
    assert strong["ret_7d"] > weak["ret_7d"]
    assert strong["rs_7d"] > 0 > weak["rs_7d"] or weak["rs_7d"] == 0
    assert strong["market_state"] == weak["market_state"]
    assert "btc_ret_7d" in strong["market_state"] and "breadth_24h" in strong["market_state"]
    # The benchmark is fetched once, not per candidate.
    assert client.calls.count("BTCUSDT") == 1


def test_a_failed_symbol_carries_none_never_a_partial_feature():
    client = _Client(fail={"WEAK"})
    selected = [{"symbol": "STRONG", "book": "CRYPTO"}, {"symbol": "WEAK", "book": "CRYPTO"}]
    _screen(client)._attach_path_features(selected)
    weak = selected[1]
    assert all(weak[k] is None for k in PATH_KEYS + RS_KEYS)
    assert selected[0]["ret_7d"] is not None


def test_a_missing_benchmark_skips_features_for_everyone():
    client = _Client(fail={"BTCUSDT"})
    selected = [{"symbol": "STRONG", "book": "CRYPTO"}]
    _screen(client)._attach_path_features(selected)
    assert all(selected[0][k] is None for k in PATH_KEYS + RS_KEYS)
    assert "market_state" not in selected[0]


def test_the_flag_is_on_because_the_replay_earned_it():
    """Switched on 2026-09-16 after the six-month replay: range_pos_7d's decile
    spread excludes zero. The bar count must exceed the 7d horizon by at least
    one, or ret_7d is always None -- the defect these tests caught."""
    cfg = load_config()
    assert cfg.agent.screen.path_features is True
    assert cfg.agent.screen.path_bars >= 169
