"""The pooled, benchmarked, calibrated experience store.

* Every venue's journal opens observations; KR/US resolve from the archive's
  parquet (never a Kiwoom call) on the venue's own hold horizon.
* A window resolves only once it is COMPLETE (a bar past its end) or the
  grace period has elapsed.
* Each resolution grades target-before-stop under the live exit contract and
  measures the book's benchmark over the same window (excess return).
* Buckets carry median and clearance rate; the paired comparison carries a
  seeded bootstrap interval; calibration bands grade stated confidence.
* The prompt block renders pooled rows plus this venue's, never another's.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.accounting.costs import CostLedger
from trading.agent.prices import ArchivePriceSource, Bar, Window
from trading.agent.scorer import ExperienceScorer, bootstrap_ci, experience_block
from trading.config import load_config

HOUR_MS = 3_600_000


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.kiwoom_archive = str(tmp_path / "archive")
    c.score.min_bucket_n = 1
    c.score.benchmarks = {"KR": "069500"}
    c.score.bootstrap_samples = 200
    c.score.resolve_grace_minutes = 180
    c.exits.markets["KR"].max_hold_minutes = 2880
    return c


def write_bars(path, start_ms: int, closes: list[float], step_ms: int = HOUR_MS, spread=0.01):
    rows = {
        "open_time": [start_ms + i * step_ms for i in range(len(closes))],
        "open": closes,
        "high": [c * (1 + spread) for c in closes],
        "low": [c * (1 - spread) for c in closes],
        "close": closes,
        "volume": [1000.0] * len(closes),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)


def _scorer(cfg):
    return ExperienceScorer(None, None, CostLedger(cfg), cfg)


def _lines(path) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def _kr_decision(ts: str) -> dict:
    return {
        "ts": ts,
        "kind": "decision",
        "market": "KR",
        "candidates": [
            {
                "symbol": "005930",
                "price": 70000.0,
                "change_pct": "+1.20",
                "volume": "1000",
                "taker_buy_share": 0.4,
                "book": "KR",
            },
            {
                "symbol": "000660",
                "price": 200000.0,
                "change_pct": "-0.50",
                "volume": "500",
                "taker_buy_share": -0.2,
                "book": "KR",
            },
        ],
        "shadow_random": "000660",
        "virtual_pick": "005930",
        "virtual_confidence": 0.52,
        "verdicts": [],
    }


def test_kiwoom_journal_opens_venue_tagged_observations(cfg, tmp_path):
    ts = dt.datetime.now(dt.UTC).isoformat()
    cfg.journal_for("KR").write_text(json.dumps(_kr_decision(ts)) + "\n", encoding="utf-8")
    stats = _scorer(cfg).run_once()
    assert stats["opened_journal"] == 2
    opens = [json.loads(x) for x in _lines(cfg.score.observations) if '"kind": "open"' in x]
    by = {(r["source"], r["symbol"]): r for r in opens}
    model = by[("model", "005930")]
    assert model["venue"] == "KR" and model["book"] == "KR"
    assert model["confidence"] == 0.52, "the stated confidence is stored for calibration"
    assert model["change_pct"] == 1.2 and model["quote_volume"] == 70000.0 * 1000
    assert model["taker_share"] == 0.4
    assert by[("shadow", "000660")]["venue"] == "KR"
    assert _scorer(cfg).run_once()["opened_journal"] == 0, "idempotent across venues"


def test_kr_observation_resolves_from_the_archive_on_its_own_horizon(cfg, tmp_path):
    opened = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2880 + 600)
    ts = opened.isoformat()
    cfg.journal_for("KR").write_text(json.dumps(_kr_decision(ts)) + "\n", encoding="utf-8")
    t0 = int(opened.timestamp() * 1000)
    # Hourly bars from t0+1h: 47 fall inside the 2-day window [t0, t0+48h),
    # the +10% drift completes on the last of them, and bars past the window
    # prove it complete.
    closes = [70000.0 * (1 + 0.10 * min(i, 46) / 46) for i in range(48)] + [77000.0] * 3
    write_bars(tmp_path / "archive/klines/kiwoom_kr/1h/005930.parquet", t0 + HOUR_MS, closes)
    write_bars(
        tmp_path / "archive/klines/kiwoom_kr/1h/000660.parquet",
        t0 + HOUR_MS,
        [200000.0 * (1 - 0.02 * min(i, 46) / 46) for i in range(48)] + [196000.0] * 3,
    )
    write_bars(
        tmp_path / "archive/klines/kiwoom_kr/1h/069500.parquet",
        t0 + HOUR_MS,
        [100.0 * (1 + 0.03 * min(i, 46) / 46) for i in range(48)] + [103.0] * 3,
    )
    scorer = _scorer(cfg)
    stats = scorer.run_once()
    assert stats["resolved"] == 2
    rows = [json.loads(x) for x in _lines(cfg.score.observations)]
    res = {r["id"]: r for r in rows if r["kind"] == "resolve"}
    model = next(r for k, r in res.items() if k.startswith("model:KR:005930"))
    assert model["forward_return_pct"] == pytest.approx(10.0, abs=0.05)
    assert model["cleared_hurdle"] is True
    assert model["benchmark_return_pct"] == pytest.approx(3.0, abs=0.1)
    assert model["excess_return_pct"] == pytest.approx(7.0, abs=0.2)
    assert model["interval"] == "1h" and model["bars"] == 47
    # KR contract: 4% stop, target from the hurdle; a +10% path with 1% bar
    # spread reaches the target before ever touching the stop.
    assert model["outcome"] == "target" and model["cleared_target"] is True
    shadow = next(r for k, r in res.items() if k.startswith("shadow:KR:000660"))
    assert shadow["outcome"] == "time" and shadow["cleared_hurdle"] is False

    store = json.loads(Path(cfg.score.experience).read_text(encoding="utf-8"))
    pairs = store["model_vs_shadow"]
    assert pairs["n"] == 1 and pairs["model_wins"] == 1
    assert store["model_vs_shadow_by_venue"]["KR"]["n"] == 1
    cal = [c for c in store["calibration"] if c["venue"] == ""]
    assert cal and cal[0]["band"] == "0.45-0.55" and cal[0]["hits"] == 1
    block = experience_block(cfg, venue="KR")
    assert "your_calibration" in block
    assert any("vs benchmark" in v for v in block["record"].values())


def test_incomplete_window_waits_for_the_archive_unless_grace_elapsed(cfg, tmp_path):
    opened = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2880 + 30)
    cfg.journal_for("KR").write_text(
        json.dumps(_kr_decision(opened.isoformat())) + "\n", encoding="utf-8"
    )
    t0 = int(opened.timestamp() * 1000)
    # Only the first day of bars is on disk: the window is not complete.
    write_bars(
        tmp_path / "archive/klines/kiwoom_kr/1h/005930.parquet", t0 + HOUR_MS, [70000.0] * 20
    )
    scorer = _scorer(cfg)
    assert scorer.run_once()["resolved"] == 0, "half a window must not be graded"
    cfg.score.resolve_grace_minutes = 10
    assert _scorer(cfg).run_once()["resolved"] == 1, "past the grace it resolves on what exists"


def test_archive_source_prefers_the_interval_that_covers_the_window(tmp_path):
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    t0 = int(start.timestamp() * 1000)
    # Hourly file stops short; daily file covers the window.
    write_bars(tmp_path / "klines/kiwoom_kr/1h/X.parquet", t0, [1.0] * 5)
    write_bars(tmp_path / "klines/kiwoom_kr/1d/X.parquet", t0, [1.0, 1.1, 1.2, 1.3], 86_400_000)
    src = ArchivePriceSource(tmp_path, "KR", ["1h", "1d"])
    win = src.window("X", start, start + dt.timedelta(days=2))
    assert win.complete and win.interval == "1d" and len(win.bars) == 2
    assert src.window("NOPE", start, start + dt.timedelta(days=2)) is None


def test_target_before_stop_walks_bars_in_order():
    bars = [Bar(0, 100, 101, 99, 100), Bar(1, 100, 112, 100, 111), Bar(2, 111, 111, 80, 90)]
    w = Window(bars=bars, complete=True)
    assert w.target_before_stop(100.0, 0.10, 0.08) == "target", "target touched first"
    w2 = Window(bars=[Bar(0, 100, 115, 90, 100)], complete=True)
    assert w2.target_before_stop(100.0, 0.10, 0.08) == "stop", "both in one bar counts as a stop"
    assert Window(bars=[Bar(0, 100, 101, 99, 100)]).target_before_stop(100.0, 0.1, 0.08) == "time"


def test_bootstrap_interval_is_seeded_and_brackets_the_mean():
    diffs = [1.0, 2.0, -0.5, 1.5, 0.5, 3.0, -1.0, 2.5]
    a = bootstrap_ci(diffs, samples=500, seed=1, level=0.95)
    b = bootstrap_ci(diffs, samples=500, seed=1, level=0.95)
    assert a == b, "same store, same interval"
    mean = sum(diffs) / len(diffs)
    assert a[0] < mean < a[1]
    assert bootstrap_ci([1.0], samples=500, seed=1, level=0.95) is None


def test_buckets_carry_median_and_excess_and_block_scopes_by_venue(cfg, tmp_path):
    ts = "2026-09-01T00:00:00+00:00"
    rows = []
    for venue, symbol, fwd, excess in (
        ("BINANCE", "AAAUSDT", 30.0, 25.0),
        ("BINANCE", "BBBUSDT", -1.0, -2.0),
        ("KR", "005930", 2.0, 1.0),
    ):
        oid = f"model:{symbol}:{ts}" if venue == "BINANCE" else f"model:{venue}:{symbol}:{ts}"
        rows.append(
            {
                "kind": "open",
                "id": oid,
                "source": "model",
                "venue": venue,
                "symbol": symbol,
                "ts": ts,
                "price": 1.0,
                "book": "CRYPTO" if venue == "BINANCE" else venue,
                "change_pct": 1.0,
                "quote_volume": 1e6,
                "taker_share": None,
            }
        )
        rows.append(
            {
                "kind": "resolve",
                "id": oid,
                "ts": ts,
                "forward_return_pct": fwd,
                "cleared_hurdle": fwd > 0.5,
                "excess_return_pct": excess,
                "cleared_target": None,
            }
        )
    with open(cfg.score.observations, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    _scorer(cfg).run_once()
    store = json.loads(Path(cfg.score.experience).read_text(encoding="utf-8"))
    pooled = next(b for b in store["buckets"] if b["label"] == "model picks")
    assert pooled["n"] == 3 and pooled["median_return_pct"] == 2.0
    assert pooled["avg_excess_pct"] == pytest.approx(8.0)
    assert pooled["clear_rate"] == pytest.approx(2 / 3, abs=1e-3)
    labels = {b["label"] for b in store["buckets"]}
    assert {"model picks:BINANCE", "model picks:KR"} <= labels
    kr = experience_block(cfg, venue="KR")["record"]
    assert "model picks (this venue)" in kr and "model picks" in kr
    assert not any("BINANCE" in k for k in kr), "another venue's split never renders"
    assert "median" in kr["model picks"] and "vs benchmark" in kr["model picks"]
