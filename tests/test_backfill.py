"""Backfill and replay tests — historical evidence with its integrity rules.

The backtest exists to shorten the road to the mainnet gate; these pin the
properties that keep it evidence rather than wishful thinking: no lookahead,
no overlap, provenance never mixed with live measurements.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trading.accounting.costs import CostLedger
from trading.agent.backfill import FLOW_HOURS, Backfiller, features_at, resolve_forward
from trading.agent.replay import Replayer, build_menu
from trading.config import load_config

MS_PER_HOUR = 3_600_000


def bars(n, price=100.0, drift=0.0, start_ms=None):
    """n hourly klines with linear drift; kline field layout as Binance sends it.

    Timestamps end just before "now" so the backfill/replay windows (which are
    anchored to the wall clock) actually cover them."""
    if start_ms is None:
        now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        start_ms = int(now.timestamp() * 1000) - n * MS_PER_HOUR
    out = []
    for i in range(n):
        p = price + drift * i
        out.append(
            [
                start_ms + i * MS_PER_HOUR,  # 0 open time
                p,  # 1 open
                p * 1.01,  # 2 high
                p * 0.99,  # 3 low
                p,  # 4 close
                1000.0,  # 5 volume
                0,  # 6 close time
                1000.0 * p,  # 7 quote volume
                10,  # 8 trades
                600.0,  # 9 taker buy base (share 0.6)
                600.0 * p,  # 10 taker buy quote
            ]
        )
    return out


class FakeClient:
    def __init__(self, bars_by_symbol):
        self.bars = bars_by_symbol

    def call(self, name, params=None):
        assert name == "klines"
        rows = [b for b in self.bars[params["symbol"]] if b[0] >= params["startTime"]]
        return SimpleNamespace(body={"rows": rows[: params.get("limit", 1000)]})


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.replay = str(tmp_path / "replay.jsonl")
    c.score.replay_summary = str(tmp_path / "replay_summary.json")
    c.score.horizon_minutes = 72 * 60
    c.score.backfill_days = 30
    return c


def test_features_never_read_future_bars():
    """Lookahead is the deadliest backtest bug: a wild future bar must change
    nothing about the features at an earlier index."""
    history = bars(200)
    f_before = features_at(history, 150)
    history[190][4] = 10_000.0  # absurd future close
    assert features_at(history, 150) == f_before


def test_resolution_reads_only_the_horizon_window():
    history = bars(400, drift=0.1)
    r = resolve_forward(history, 100, 72, float(history[100][4]))
    assert r is not None
    expected = float(history[172][4]) / float(history[100][4]) - 1
    assert r["forward_return_pct"] == pytest.approx(expected * 100, abs=1e-3)
    assert resolve_forward(history, 390, 72, 100.0) is None, "short windows resolve to nothing"


def test_backfill_writes_backtest_observations_without_overlap(cfg):
    pool = [
        {"symbol": "AAAUSDT", "book": "CRYPTO", "price": 100, "change_pct": 1, "quote_volume": 1e6}
    ]
    screen = SimpleNamespace(tradable_pool=lambda size: pool)
    client = FakeClient({"AAAUSDT": bars(FLOW_HOURS + 72 * 4 + 10, drift=0.05)})
    bf = Backfiller(client, screen, CostLedger(cfg), cfg)
    stats = bf.run(days=30)
    assert stats["opened"] > 0

    rows = [json.loads(x) for x in open(cfg.score.observations, encoding="utf-8")]
    opens = [r for r in rows if r["kind"] == "open"]
    assert all(r["source"] == "backtest" for r in opens), "provenance must be explicit"
    stamps = sorted(dt.datetime.fromisoformat(r["ts"]) for r in opens)
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 72 for g in gaps), "observations must be one horizon apart (no overlap)"
    # Every open has its resolve, written in the same pass.
    resolves = {r["id"] for r in rows if r["kind"] == "resolve"}
    assert {r["id"] for r in opens} <= resolves

    assert bf.run(days=30)["opened"] == 0, "backfill must be idempotent"


def test_backtest_buckets_stay_separate_from_live(cfg, tmp_path):
    """Backtest aggregates carry the `backtest` label; live `universe` buckets
    must never silently absorb reconstructed history."""
    from trading.agent.scorer import ExperienceScorer

    pool = [
        {"symbol": "AAAUSDT", "book": "CRYPTO", "price": 100, "change_pct": 1, "quote_volume": 1e6}
    ]
    screen = SimpleNamespace(tradable_pool=lambda size: pool, _flow=lambda s: {})
    client = FakeClient({"AAAUSDT": bars(FLOW_HOURS + 72 * 4 + 10, drift=0.05)})
    ledger = CostLedger(cfg)
    Backfiller(client, screen, ledger, cfg).run(days=30)

    cfg.agent.journal = str(tmp_path / "journal.jsonl")
    scorer = ExperienceScorer(
        client, SimpleNamespace(tradable_pool=lambda s: [], _flow=lambda s: {}), ledger, cfg
    )
    exp = scorer.run_once()
    data = json.loads(open(cfg.score.experience, encoding="utf-8").read())
    labels = [b["label"] for b in data["buckets"]]
    assert any(label.startswith("backtest 24h-change") for label in labels)
    universe_bands = [b for b in data["buckets"] if b["label"].startswith("universe 24h-change")]
    assert all(b["n"] == 0 for b in universe_bands), "live buckets must stay empty"
    assert exp["buckets"] > 0


def test_replay_pairs_model_against_random_from_the_same_menu(cfg):
    """The replay asks the real prompt, applies the real parser, and both
    picks resolve from the same future bars."""
    pool = [
        {"symbol": "UPUSDT", "book": "CRYPTO", "price": 100, "change_pct": 20, "quote_volume": 9e6},
        {
            "symbol": "DOWNUSDT",
            "book": "CRYPTO",
            "price": 100,
            "change_pct": 20,
            "quote_volume": 8e6,
        },
    ]
    screen = SimpleNamespace(tradable_pool=lambda size: pool)
    length = FLOW_HOURS + 72 * 6
    client = FakeClient(
        {
            "UPUSDT": bars(length, drift=0.2),
            "DOWNUSDT": bars(length, drift=-0.1),
        }
    )
    cfg.agent.screen.min_change_pct = 0.0
    cfg.agent.screen.max_change_pct = 0.0

    class FakeLLM:
        calls = 0

        def ask(self, prompt, system=None, tier=None):
            FakeLLM.calls += 1
            return json.dumps(
                {
                    "intents": [],
                    "best_candidate": {"symbol": "UPUSDT", "confidence": 0.5},
                    "commentary": "replay",
                }
            )

    replayer = Replayer(client, screen, cfg, llm=FakeLLM(), rng=__import__("random").Random(1))
    summary = replayer.run(decisions=4)
    assert summary["n"] >= 1
    assert FakeLLM.calls <= 4, "--decisions is a hard billing cap"
    assert summary["model_avg_pct"] > 0, "the model picked the rising name"
    rows = [json.loads(x) for x in open(cfg.score.replay, encoding="utf-8")]
    assert all(r["model"] == "UPUSDT" for r in rows)
    assert all(r["shadow"] in {"UPUSDT", "DOWNUSDT"} for r in rows)


def test_menu_is_built_without_lookahead(cfg):
    history = bars(FLOW_HOURS + 100)
    by_symbol = {"AAAUSDT": ("CRYPTO", history)}
    cfg.agent.screen.min_change_pct = 0.0
    cfg.agent.screen.max_change_pct = 0.0
    menu_before = build_menu(by_symbol, FLOW_HOURS + 10, cfg)
    history[FLOW_HOURS + 50][4] = 99_999.0  # wild future bar
    assert build_menu(by_symbol, FLOW_HOURS + 10, cfg) == menu_before
