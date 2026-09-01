"""KR/US archive backfill tests: provenance, overlap, lookahead, flow share."""

from __future__ import annotations

import datetime as dt
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.agent.backfill_kiwoom import KiwoomBackfiller, flow_share
from trading.config import load_config

DAY_MS = 86_400_000


def write_klines(path, n, price=10_000.0, drift=10.0):
    now = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(now.timestamp() * 1000) - n * DAY_MS
    rows = {
        "open_time": [start + i * DAY_MS for i in range(n)],
        "open": [price + drift * i for i in range(n)],
        "high": [(price + drift * i) * 1.02 for i in range(n)],
        "low": [(price + drift * i) * 0.98 for i in range(n)],
        "close": [price + drift * i for i in range(n)],
        "volume": [10_000.0] * n,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)
    return rows


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.kiwoom_archive = str(tmp_path / "archive")
    c.score.horizon_minutes = 4320  # -> 3 daily bars
    c.score.backfill_days = 120
    return c


def test_backfill_reads_archive_and_writes_kr_provenance(cfg, tmp_path):
    write_klines(tmp_path / "archive/klines/kiwoom_kr/1d/005930.parquet", 100)
    stats = KiwoomBackfiller(cfg).run(market="KR", days=120, symbols=10)
    assert stats["opened"] > 0

    rows = [json.loads(x) for x in open(cfg.score.observations, encoding="utf-8")]
    opens = [r for r in rows if r["kind"] == "open"]
    assert all(r["source"] == "backtest_kr" and r["book"] == "KR" for r in opens)
    stamps = sorted(dt.datetime.fromisoformat(r["ts"]) for r in opens)
    gaps = [(b - a).days for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 3 for g in gaps), "one horizon (3 daily bars) between observations"
    # Rising series with a KR hurdle of 0.28%: forward returns must clear it.
    resolves = [r for r in rows if r["kind"] == "resolve"]
    assert all(r["hurdle_pct"] == pytest.approx(0.28) for r in resolves)
    assert KiwoomBackfiller(cfg).run(market="KR", days=120, symbols=10)["opened"] == 0, "idempotent"


def test_flow_share_is_unit_free_and_bounded():
    flow = {"2026-01-01": (50.0, 100.0), "2026-01-02": (-30.0, 60.0)}
    s = flow_share(flow, ["2026-01-01", "2026-01-02"])
    assert s == pytest.approx((50 - 30) / 160)
    assert flow_share(flow, ["2099-01-01"]) is None, "no window, no fabricated signal"
    # Scaling every value 1000x (value units vs share units) changes nothing.
    scaled = {k: (a * 1000, b * 1000) for k, (a, b) in flow.items()}
    assert flow_share(scaled, ["2026-01-01", "2026-01-02"]) == pytest.approx(s)


def test_features_ignore_future_bars(cfg, tmp_path):
    """Corrupting bars after the cutoff date must not change what was opened
    before it — the per-observation window is bounded at its own bar."""
    p = tmp_path / "archive/klines/kiwoom_kr/1d/000660.parquet"
    write_klines(p, 100)
    KiwoomBackfiller(cfg).run(market="KR", days=120, symbols=10)
    first = [json.loads(x) for x in open(cfg.score.observations, encoding="utf-8")]
    opens = {r["id"]: r for r in first if r["kind"] == "open"}
    # Rewrite the file with an absurd LAST bar and rerun into a fresh store.
    rows = pq.read_table(p).to_pylist()
    rows[-1]["close"] = 9_999_999.0
    pq.write_table(pa.table({k: [r[k] for r in rows] for k in rows[0]}), p)
    cfg.score.observations = str(tmp_path / "observations2.jsonl")
    KiwoomBackfiller(cfg).run(market="KR", days=120, symbols=10)
    second = [json.loads(x) for x in open(cfg.score.observations, encoding="utf-8")]
    opens2 = {r["id"]: r for r in second if r["kind"] == "open"}
    shared = set(opens) & set(opens2)
    assert shared
    assert all(opens[i]["change_pct"] == opens2[i]["change_pct"] for i in shared)
