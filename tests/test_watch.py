"""Watcher seam tests.

The defect class this guards: run_service polled Telegram commands under
--broker binance but built no watcher to answer them, so every command was
consumed and dropped silently. The seam is build_watcher; these tests pin that
the binance path constructs a watcher whose commands act on the right files and
whose /status runs against the Binance account shape without Kiwoom assumptions.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from trading.brokers.binance.client import BinanceClient
from trading.config import BinanceSecrets, load_config
from trading.watch import BinanceWatcher, build_watcher


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.broker.binance.use_testnet = False
    c.broker.binance.allow_orders = False
    c.broker.binance.min_call_interval_s = 0
    c.risk.kill_switch_file = str(tmp_path / "HALT")
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.exits.state = str(tmp_path / "exit_policy.json")
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    return c


def _sym(symbol, base):
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": base,
        "quoteAsset": "USDT",
        "filters": [],
        "permissionSets": [[]],
    }


def make_client(cfg):
    # _env_file=None: never let the live Binance keys into a test or its output.
    secrets = BinanceSecrets(
        _env_file=None,
        BINANCE_MAINNET_API_KEY="test-key",
        BINANCE_MAINNET_SECRET_KEY="test-secret",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/api/v3/time":
                return httpx.Response(200, json={"serverTime": 1786000000000})
            case "/api/v3/account":
                # One managed holding (BTC), one seed balance (TUT), quote cash.
                return httpx.Response(
                    200,
                    json={
                        "balances": [
                            {"asset": "USDT", "free": "100.0", "locked": "7.5"},
                            {"asset": "BTC", "free": "0.5", "locked": "0"},
                            {"asset": "TUT", "free": "18446", "locked": "0"},
                        ]
                    },
                )
            case "/api/v3/exchangeInfo":
                return httpx.Response(
                    200, json={"symbols": [_sym("BTCUSDT", "BTC"), _sym("TUTUSDT", "TUT")]}
                )
            case "/api/v3/ticker/price":
                return httpx.Response(
                    200,
                    json=[
                        {"symbol": "BTCUSDT", "price": "80000"},
                        {"symbol": "TUTUSDT", "price": "0.03"},
                    ],
                )
        return httpx.Response(200, json={"rows": []})

    return BinanceClient(
        "CRYPTO",
        cfg=cfg,
        secrets=secrets,
        allow_orders=False,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def write_plan(tmp_path, symbol="BTCUSDT", entry=70000.0):
    plan = {
        "symbol": symbol,
        "entry_price": entry,
        "quantity": 0.5,
        "opened_at": dt.datetime.now(dt.UTC).isoformat(),
        "stop": entry * 0.92,
        "net_breakeven": entry * 1.006,
        "target": entry * 1.05,
        "high_water": entry,
        "api_cost_share": 0.0,
    }
    (tmp_path / "exit_policy_BINANCE.json").write_text(
        json.dumps({"plans": {symbol: plan}}), encoding="utf-8"
    )


def make_watcher(cfg):
    return BinanceWatcher(cfg=cfg, client=make_client(cfg), notifier=object())


def test_build_watcher_routes_binance(cfg, monkeypatch):
    mock = make_client(cfg)
    monkeypatch.setattr("trading.watch.BinanceClient", lambda *a, **k: mock)
    w = build_watcher("binance", "BINANCE", cfg=cfg, notifier=object())
    assert isinstance(w, BinanceWatcher)
    # /halt and /resume must act on the file the GATE checks for this venue.
    assert w.halt_file.name == "HALT.BINANCE"


def test_halt_and_resume_act_on_the_venue_file(cfg, tmp_path):
    w = make_watcher(cfg)
    w.handle("/halt")
    assert (tmp_path / "HALT.BINANCE").exists()
    assert not (tmp_path / "HALT").exists(), "venue halt must not set the global switch"
    w.handle("/resume")
    assert not (tmp_path / "HALT.BINANCE").exists()


def test_resume_warns_when_global_halt_still_set(cfg, tmp_path):
    (tmp_path / "HALT").write_text("emergency stop\n", encoding="utf-8")
    w = make_watcher(cfg)
    w.handle("/halt")
    reply = w.handle("/resume")
    # Clearing the venue file while data/HALT stands must not claim orders flow.
    assert "global kill switch" in reply


def test_status_reads_the_binance_account(cfg):
    w = make_watcher(cfg)
    reply = w.handle("/status")
    assert "account" in reply
    assert "⚠️" not in reply, f"status path failed: {reply}"


def test_status_carries_the_exit_plan_detail(cfg, tmp_path):
    """The Binance app cannot show this account, so the bot must: the report
    carries stop/breakeven/target from the persisted plan, live marks, and
    collapses seed balances into a single unmanaged summary line."""
    write_plan(tmp_path, entry=70000.0)
    reply = make_watcher(cfg).handle("/status")
    assert "BTCUSDT" in reply and "stop" in reply and "target" in reply
    assert "+14.29%" in reply, "mark 80,000 against entry 70,000"
    assert "unmanaged: 1 balance" in reply, "the seed TUT must be one summary line"
    assert "TUTUSDT" not in reply.split("unmanaged")[0], "seeds never listed as positions"
    assert "free 100.00 · locked 7.50" in reply


def test_positions_and_cash_are_scoped_sections(cfg, tmp_path):
    write_plan(tmp_path)
    w = make_watcher(cfg)
    positions = w.handle("/positions")
    cash = w.handle("/cash")
    assert "Managed positions" in positions and "Cash" not in positions
    assert "Cash" in cash and "Managed positions" not in cash
    assert "DeepSeek" not in positions and "DeepSeek" not in cash
    assert "Learning" not in positions and "Learning" not in cash


def test_status_reports_pnl_from_ledger_lots_not_balances(cfg):
    """Unrealised P&L marks only the units the ledger says were BOUGHT — broker
    balances include seed units nobody paid for, and marking those would invent
    profit out of free inventory."""
    from trading.accounting.costs import CostLedger

    ledger = CostLedger(cfg)
    # Open lot: 0.5 BTC @ 70,000; mock mark is 80,000 -> +5,000 unrealised.
    ledger.record_trade(symbol="BTCUSDT", side="BUY", quantity=0.5, price=70000, market="CRYPTO")
    # A closed round trip: +10 realised. The mock account also holds 18,446
    # seed TUT that must contribute nothing.
    ledger.record_trade(symbol="TUTUSDT", side="BUY", quantity=100, price=1.0, market="CRYPTO")
    ledger.record_trade(symbol="TUTUSDT", side="SELL", quantity=100, price=1.1, market="CRYPTO")

    w = make_watcher(cfg)
    reply = w.handle("/status")
    assert "P&L" in reply
    assert "unrealised: +5,000.00 USDT" in reply
    assert "realised  : +10.00 USDT" in reply

    pnl = w.handle("/pnl")
    assert "P&L" in pnl and "Cash" not in pnl


def test_status_shows_deepseek_spend_against_budget(cfg):
    """The decide loop stops when the daily budget is spent, so the burn rate
    is part of the operator's default view, broken down per model."""
    from trading.accounting.costs import CostLedger, Usage

    ledger = CostLedger(cfg)
    ledger.record_llm(
        Usage(
            model="deepseek-chat",
            provider="deepseek",
            tier="fast",
            input_tokens=4000,
            output_tokens=600,
            usd=ledger.price_call("deepseek-chat", 4000, 600),
        )
    )
    reply = make_watcher(cfg).handle("/status")
    assert "DeepSeek spend" in reply
    assert "deepseek-chat: 1 calls" in reply
    assert "budget" in reply
    assert "Learning" in reply, "the corpus progress belongs in the default view"
