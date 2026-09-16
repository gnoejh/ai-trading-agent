"""Daily path features for every KR backtest observation, from the archive parquet.

    uv run python -m trading.agent.backfill_features_kr

Zero Kiwoom calls: the ai-trading-history archive's daily bars are read
directly (they carry volume, which the resolver's `Bar` drops), so this runs
at any hour without touching the downloader's token. Features are the DAILY
definitions in `features.py` -- the same code the KR screen will run in
session -- computed from bars up to the observation's own day and no further;
relative strength is against the venue benchmark (`score.benchmarks.KR`) on
the same day. Side-file keyed by observation id; resumable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from trading.agent.backfill_features import _done
from trading.agent.features import (
    DAILY_PATH_KEYS,
    daily_path_features,
    relative_daily,
    turnover_ratio_daily,
)
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DBar:
    t: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _daily(root: Path, venue_dir: str, symbol: str) -> list[DBar]:
    path = root / "klines" / venue_dir / "1d" / f"{symbol}.parquet"
    if not path.exists():
        return []
    import pyarrow.parquet as pq

    rows = pq.read_table(path).to_pylist()
    bars = [
        DBar(
            int(r["open_time"]),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r.get("volume") or 0),
        )
        for r in rows
        if r.get("open_time") is not None and float(r["close"]) > 0
    ]
    bars.sort(key=lambda b: b.t)
    return bars


def _day(ts: str) -> str:
    return dt.datetime.fromisoformat(ts).astimezone(dt.UTC).date().isoformat()


def _index_by_day(bars: list[DBar]) -> dict[str, int]:
    return {
        dt.datetime.fromtimestamp(b.t / 1000, dt.UTC).date().isoformat(): i
        for i, b in enumerate(bars)
    }


def _targets(path: Path, source: str) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "open" and r.get("source") == source:
                out.setdefault(r["symbol"], []).append((r["id"], r["ts"]))
    return out


def run(cfg: AppConfig | None = None, *, venue: str = "KR", symbols: list[str] | None = None) -> dict:
    cfg = cfg or config()
    source = {"KR": "backtest_kr", "US": "backtest_us"}[venue]
    venue_dir = {"KR": "kiwoom_kr", "US": "kiwoom_us"}[venue]
    root = Path(cfg.score.kiwoom_archive)
    bench_symbol = cfg.score.benchmarks[venue]
    out_path = Path(cfg.score.backtest_features_by_venue[venue])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = _done(out_path)
    targets = _targets(Path(cfg.score.observations), source)
    if symbols:
        targets = {s: targets[s] for s in symbols if s in targets}

    bench = _daily(root, venue_dir, bench_symbol)
    bench_idx = _index_by_day(bench)
    if not bench:
        raise SystemExit(f"benchmark {bench_symbol} has no daily bars in the archive")

    written = skipped = missing = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for n, (symbol, rows) in enumerate(sorted(targets.items()), start=1):
            pending = [(oid, ts) for oid, ts in rows if oid not in done]
            if not pending:
                skipped += len(rows)
                continue
            bars = _daily(root, venue_dir, symbol)
            if not bars:
                missing += 1
                continue
            idx = _index_by_day(bars)
            for oid, ts in pending:
                day = _day(ts)
                i, j = idx.get(day), bench_idx.get(day)
                if i is None or j is None:
                    continue
                feats = daily_path_features(bars, i)
                if not feats:
                    continue
                feats.update(relative_daily(feats, daily_path_features(bench, j)))
                turnovers = [b.close * b.volume for b in bars[: i + 1]]
                feats["turnover_ratio_5d"] = turnover_ratio_daily(turnovers)
                row = {"id": oid, "symbol": symbol, "ts": ts}
                row.update({k: feats.get(k) for k in DAILY_PATH_KEYS})
                row.update({k: v for k, v in feats.items() if k.startswith("rs_")})
                fh.write(json.dumps(row) + "\n")
                written += 1
            if n % 100 == 0:
                log.info("%s features: %d/%d symbols, %d rows", venue, n, len(targets), written)
    stats = {"written": written, "skipped_existing": skipped, "symbols_without_bars": missing}
    log.info("backfill_features_%s: %s", venue, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--venue", default="KR", choices=("KR", "US"))
    ap.add_argument("--symbols", nargs="*")
    args = ap.parse_args(argv)
    print(run(config(), venue=args.venue, symbols=args.symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
