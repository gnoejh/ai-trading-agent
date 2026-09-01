"""Mainnet promotion gate tests.

The repository's purpose as arithmetic: promote when measured profit is
positive, stay on testnet otherwise. These pin the criteria so a config tweak
cannot silently loosen the bar real money is admitted through.
"""

from __future__ import annotations

import json

import pytest

from trading.accounting.costs import CostLedger
from trading.agent.promotion import evaluate, render
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.trade_since = ""
    c.score.trade_markets = ["CRYPTO"]
    c.promotion.min_closed_trades = 2
    c.promotion.min_shadow_pairs = 2
    c.promotion.since = ""
    return c


def _round_trip(ledger, symbol, entry, exit_price, qty=10.0):
    ledger.record_trade(symbol=symbol, side="BUY", quantity=qty, price=entry, market="CRYPTO")
    ledger.record_trade(symbol=symbol, side="SELL", quantity=qty, price=exit_price, market="CRYPTO")


def _pairs(cfg, n, model, shadow):
    with open(cfg.score.experience, "w", encoding="utf-8") as fh:
        json.dump(
            {"model_vs_shadow": {"n": n, "model_avg_pct": model, "shadow_avg_pct": shadow}}, fh
        )


def test_empty_history_is_not_ready(cfg):
    result = evaluate(cfg)
    assert not result["ready"]
    assert "not yet" in render(cfg)


def test_gross_profit_below_costs_stays_on_testnet(cfg):
    """+0.2% per trip loses to a 0.5% hurdle: the gate must charge costs."""
    ledger = CostLedger(cfg)
    _round_trip(ledger, "AAAUSDT", 100.0, 100.2)
    _round_trip(ledger, "BBBUSDT", 100.0, 100.2)
    _pairs(cfg, 5, 1.0, 0.5)
    result = evaluate(cfg)
    assert not result["ready"]
    net_check = next(c for c in result["checks"] if c["name"].startswith("net P&L"))
    assert not net_check["ok"], "gross gains under the hurdle are losses"


def test_all_criteria_met_reports_ready(cfg):
    ledger = CostLedger(cfg)
    _round_trip(ledger, "AAAUSDT", 100.0, 103.0)  # +3% clears 0.5% comfortably
    _round_trip(ledger, "BBBUSDT", 100.0, 102.0)
    _pairs(cfg, 5, 1.0, 0.2)
    result = evaluate(cfg)
    assert result["ready"], [c for c in result["checks"] if not c["ok"]]
    assert "use_testnet" in render(cfg), "the verdict must name the owner's switch"


def test_model_must_beat_its_shadow(cfg):
    """Positive P&L with no edge over random is luck plus a rally — not a
    reason to trade real money."""
    ledger = CostLedger(cfg)
    _round_trip(ledger, "AAAUSDT", 100.0, 103.0)
    _round_trip(ledger, "BBBUSDT", 100.0, 102.0)
    _pairs(cfg, 5, 0.2, 1.0)  # random wins
    assert not evaluate(cfg)["ready"]


def test_since_scopes_the_measurement_epoch(cfg):
    """Trades from a dead regime (a sprint, pre-reset) must not decide the
    verdict for the configuration that would actually trade on mainnet."""
    ledger = CostLedger(cfg)
    _round_trip(ledger, "AAAUSDT", 100.0, 110.0)  # winner, but before `since`
    _pairs(cfg, 5, 1.0, 0.2)
    cfg.promotion.since = "2999-01-01"
    result = evaluate(cfg)
    n_check = next(c for c in result["checks"] if "round trips" in c["name"])
    assert not n_check["ok"], "epoch-excluded trades must not count"
