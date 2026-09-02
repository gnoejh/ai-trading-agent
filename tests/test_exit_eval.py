"""Exit counterfactuals replay the live policy arithmetic over real trips."""

from __future__ import annotations

import datetime as dt
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.accounting.costs import CostLedger
from trading.agent.exit_eval import ExitEvaluator, policy_for, simulate
from trading.agent.prices import Bar, Window
from trading.config import load_config

HOUR_MS = 3_600_000


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.score.kiwoom_archive = str(tmp_path / "archive")
    c.exit_eval.output = str(tmp_path / "exit_eval.json")
    c.exit_eval.holds_minutes = [1440, 4320]
    c.exit_eval.stops_pct = [0.04, 0.12]
    c.promotion.since = ""
    c.score.trade_since = ""
    return c


def _window(start: dt.datetime, closes, spread=0.005):
    t0 = int(start.timestamp() * 1000)
    bars = [
        Bar(t0 + (i + 1) * HOUR_MS, c, c * (1 + spread), c * (1 - spread), c)
        for i, c in enumerate(closes)
    ]
    return Window(bars=bars, complete=True, interval="1h")


def test_policy_override_changes_only_stop_and_hold(cfg):
    base = cfg.exits.for_market("BINANCE")
    policy = policy_for(cfg, "BINANCE", stop_pct=0.12, hold_minutes=1440)
    assert policy.ecfg.stop_loss_pct == 0.12 and policy.ecfg.max_hold_minutes == 1440
    assert policy.ecfg.min_reward_risk == base.min_reward_risk
    assert policy.ecfg.target_hurdle_multiple == base.target_hurdle_multiple
    assert cfg.exits.for_market("BINANCE").stop_loss_pct == base.stop_loss_pct, (
        "the live config is untouched"
    )


def test_simulation_stops_targets_and_times_out_like_the_supervisor(cfg):
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    tight = policy_for(cfg, "BINANCE", stop_pct=0.04, hold_minutes=4320)
    wide = policy_for(cfg, "BINANCE", stop_pct=0.12, hold_minutes=4320)
    # Dips 6% first, then rallies 20%: a 4% stop is hit, a 12% stop rides to target.
    closes = [100, 97, 94, 96, 100, 106, 112, 118, 124, 130]
    win = _window(start, closes)
    assert simulate(win, 100.0, start, tight, 4320)["reason"] == "stop"
    ride = simulate(win, 100.0, start, wide, 4320)
    assert ride["reason"] == "target" and ride["exit_price"] > 100.0
    # Flat path: time stop at the hold, at the bar close.
    flat = simulate(_window(start, [100.0] * 30), 100.0, start, wide, 1440)
    assert flat["reason"] == "time" and flat["minutes"] >= 1440


def test_grid_replays_ledger_trips_from_the_venue_price_record(cfg, tmp_path):
    ledger = CostLedger(cfg)
    opened = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
    # Write ledger rows with explicit timestamps.
    rows = [
        {
            "ts": opened.isoformat(),
            "kind": "trade",
            "symbol": "005930",
            "side": "BUY",
            "quantity": 10,
            "price": 100.0,
            "market": "KR",
        },
        {
            "ts": (opened + dt.timedelta(hours=30)).isoformat(),
            "kind": "trade",
            "symbol": "005930",
            "side": "SELL",
            "quantity": 10,
            "price": 103.0,
            "market": "KR",
        },
    ]
    with open(cfg.accounting.ledger, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    closed = ledger.closed_trades()
    assert closed[0]["entry_ts"] == opened.isoformat() and closed[0]["exit_ts"]
    t0 = int(opened.timestamp() * 1000)
    closes = [100 + 1.0 * i for i in range(100)]  # steady +1%/hour
    path = tmp_path / "archive/klines/kiwoom_kr/1h/005930.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "open_time": [t0 + (i + 1) * HOUR_MS for i in range(len(closes))],
                "open": closes,
                "high": [c * 1.002 for c in closes],
                "low": [c * 0.998 for c in closes],
                "close": closes,
                "volume": [1.0] * len(closes),
            }
        ),
        path,
    )
    result = ExitEvaluator(cfg, client=None).run()
    assert result["replayed"] == 1 and result["skipped"] == 0
    assert result["actual"]["n"] == 1
    grid = {(g["hold_minutes"], g["stop_pct"]): g for g in result["grid"]}
    assert set(grid) == {(1440, 0.04), (1440, 0.12), (4320, 0.04), (4320, 0.12)}
    # On a steady rise every cell ends net positive; the 4% stop cells (a
    # ~8.8% KR target) reach it within hours, the 12% cells (~24.8% target)
    # time out at the 24h hold and ride to target on the 72h hold.
    assert all(g["avg_net_pct"] > 0 for g in grid.values())
    assert grid[(1440, 0.04)]["exits"] == {"target": 1}
    assert grid[(4320, 0.04)]["exits"] == {"target": 1}
    assert grid[(1440, 0.12)]["exits"] == {"time": 1}
    assert grid[(4320, 0.12)]["exits"] == {"target": 1}
    assert json.loads((tmp_path / "exit_eval.json").read_text(encoding="utf-8"))["replayed"] == 1
