"""Compute the path/RS features for every backtest observation already on disk.

    uv run python -m trading.agent.backfill_features

The observation log is append-only and stays untouched: this writes a
side-file keyed by observation id (`score.backtest_features`) that the screen
replay joins at read time. Resumable — ids already present are skipped — so a
failed symbol costs one retry, not a rerun.

Bars come from the mainnet data plane, hourly, one paginated fetch per symbol
(BTCUSDT first and kept, so every name's relative strength is against the same
bars). Features are computed by `trading.agent.features` from bars up to the
observation's own bar and no further.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path

from trading.agent.backfill import fetch_hourly
from trading.agent.features import path_features, relative
from trading.brokers.binance.client import BinanceClient
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

MS_PER_HOUR = 3_600_000
LOOKBACK_H = 168 + 24  # a week of history plus a day's slack before the first observation
BENCHMARK = "BTCUSDT"


def _load_targets(path: Path) -> dict[str, list[tuple[str, str]]]:
    """symbol -> [(obs_id, ts)] for every backtest open."""
    out: dict[str, list[tuple[str, str]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "open" and r.get("source") == "backtest":
                out.setdefault(r["symbol"], []).append((r["id"], r["ts"]))
    return out


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def _index(bars: list[list]) -> dict[int, int]:
    return {int(b[0]): i for i, b in enumerate(bars)}


def _ts_ms(ts: str) -> int:
    return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)


def run(cfg: AppConfig | None = None, *, symbols: list[str] | None = None) -> dict:
    cfg = cfg or config()
    targets = _load_targets(Path(cfg.score.observations))
    if symbols:
        targets = {s: targets[s] for s in symbols if s in targets}
    out_path = Path(cfg.score.backtest_features)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = _done(out_path)
    client = BinanceClient("CRYPTO", cfg=cfg)

    all_ts = [ts for rows in targets.values() for _, ts in rows]
    start_ms = min(_ts_ms(t) for t in all_ts) - LOOKBACK_H * MS_PER_HOUR
    end_ms = max(_ts_ms(t) for t in all_ts) + MS_PER_HOUR

    btc_bars = fetch_hourly(client, BENCHMARK, start_ms, end_ms)
    btc_idx = _index(btc_bars)
    log.info("benchmark bars: %d", len(btc_bars))

    written = skipped = failed = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for n, (symbol, rows) in enumerate(sorted(targets.items()), start=1):
            pending = [(oid, ts) for oid, ts in rows if oid not in done]
            if not pending:
                skipped += len(rows)
                continue
            try:
                bars = (
                    btc_bars
                    if symbol == BENCHMARK
                    else fetch_hourly(client, symbol, start_ms, end_ms)
                )
            except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
                log.warning("features %s failed: %s", symbol, exc)
                failed += 1
                continue
            idx = _index(bars)
            for oid, ts in pending:
                ms = _ts_ms(ts)
                i, j = idx.get(ms), btc_idx.get(ms)
                if i is None or j is None:
                    continue
                feats = path_features(bars, i)
                if not feats:
                    continue
                feats.update(relative(feats, path_features(btc_bars, j)))
                fh.write(json.dumps({"id": oid, "symbol": symbol, "ts": ts, **feats}) + "\n")
                written += 1
            if n % 20 == 0:
                log.info("features: %d/%d symbols, %d rows", n, len(targets), written)
    stats = {"written": written, "skipped_existing": skipped, "failed_symbols": failed}
    log.info("backfill_features: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="*", help="restrict to these symbols (for a quick check)")
    args = ap.parse_args(argv)
    stats = run(config(), symbols=args.symbols)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
