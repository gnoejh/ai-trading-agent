"""Live perp funding on candidates: one call, None where there is no perp."""

from __future__ import annotations

from types import SimpleNamespace

from trading.brokers.binance.universe import BinanceScreen
from trading.config import load_config


class _Client:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def call(self, name, params):
        self.calls.append(name)
        if self.fail:
            raise RuntimeError("fapi down")
        assert name == "premium_index" and params == {}
        return SimpleNamespace(
            body={
                "rows": [
                    {"symbol": "AAAUSDT", "lastFundingRate": "0.0010"},
                    {"symbol": "BBBUSDT", "lastFundingRate": "-0.0002"},
                    {"symbol": "ZZZUSDT", "lastFundingRate": "not a number"},
                ]
            }
        )


def _screen(client):
    cfg = load_config()
    cfg.agent.screen.funding_features = True
    return BinanceScreen(client, SimpleNamespace(), cfg)


def test_one_call_serves_every_candidate_and_absent_perps_carry_none():
    client = _Client()
    selected = [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}, {"symbol": "NOPERPUSDT"}]
    _screen(client)._attach_funding(selected)
    assert client.calls == ["premium_index"]
    assert selected[0]["funding_rate_pct"] == 0.1  # 0.0010 -> 0.10 %
    assert selected[1]["funding_rate_pct"] == -0.02
    assert selected[2]["funding_rate_pct"] is None


def test_a_failed_read_carries_none_for_everyone():
    selected = [{"symbol": "AAAUSDT"}]
    _screen(_Client(fail=True))._attach_funding(selected)
    assert selected[0]["funding_rate_pct"] is None


def test_the_flag_is_on_because_the_replay_earned_it():
    cfg = load_config()
    assert cfg.agent.screen.funding_features is True
    assert "funding_low" in cfg.score.arms and "funding_high" in cfg.score.arms
