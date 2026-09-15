"""Perp funding rate at the time of every backtest observation.

    uv run python -m trading.agent.backfill_funding

Positioning is the one thing an expert watches that price bars do not carry:
crowded longs (high positive funding) tend to precede underperformance, and
the USDT-M perpetual market publishes funding every 8 hours with a free
history deep enough to validate over the whole backtest corpus. Open interest
history stops at 30 days and so cannot be validated to this repo's standard;
it is not fetched.

One `fundingRate` call per symbol covers the corpus (8h cadence, limit 1000).
A spot name without a perp simply has no funding feature. Written as a
side-file keyed by observation id, like the path features; resumable.
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
from pathlib import Path

from trading.agent.backfill_features import _done, _load_targets, _ts_ms
from trading.brokers.binance.client import BinanceClient, BinanceError
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

MS_8H = 8 * 3_600_000
AVG_PERIODS = 9  # 3 days of 8h fundings


def fetch_funding(
    client: BinanceClient, symbol: str, start_ms: int, end_ms: int
) -> list[tuple[int, float]]:
    """(funding_time_ms, rate) ascending, paginated past the 1000 limit."""
    out: list[tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        rows = client.call(
            "funding_history",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        ).body.get("rows", [])
        if not rows:
            break
        for r in rows:
            out.append((int(r["fundingTime"]), float(r["fundingRate"])))
        cursor = out[-1][0] + 1
        if len(rows) < 1000:
            break
    return out


def funding_at(series: list[tuple[int, float]], times: list[int], ts_ms: int) -> dict | None:
    """The last funding settled at or before ts, and the 3-day average before it."""
    i = bisect.bisect_right(times, ts_ms)
    if i == 0:
        return None
    last = series[i - 1][1]
    window = [r for _, r in series[max(0, i - AVG_PERIODS) : i]]
    return {
        # Per-8h rates in %: 0.01% is the neutral default; 0.1% is crowded.
        "funding_rate_pct": round(last * 100, 5),
        "funding_3d_avg_pct": round(sum(window) / len(window) * 100, 5),
    }


def run(cfg: AppConfig | None = None, *, symbols: list[str] | None = None) -> dict:
    cfg = cfg or config()
    targets = _load_targets(Path(cfg.score.observations))
    if symbols:
        targets = {s: targets[s] for s in symbols if s in targets}
    out_path = Path(cfg.score.backtest_funding)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = _done(out_path)
    client = BinanceClient("CRYPTO", cfg=cfg)
    all_ts = [ts for rows in targets.values() for _, ts in rows]
    start_ms = min(_ts_ms(t) for t in all_ts) - AVG_PERIODS * MS_8H
    end_ms = max(_ts_ms(t) for t in all_ts) + MS_8H

    written = skipped = no_perp = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for n, (symbol, rows) in enumerate(sorted(targets.items()), start=1):
            pending = [(oid, ts) for oid, ts in rows if oid not in done]
            if not pending:
                skipped += len(rows)
                continue
            try:
                series = fetch_funding(client, symbol, start_ms, end_ms)
            except BinanceError as exc:
                log.info("no perp for %s (%s)", symbol, exc)
                no_perp += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
                log.warning("funding %s failed: %s", symbol, exc)
                continue
            if not series:
                no_perp += 1
                continue
            times = [t for t, _ in series]
            for oid, ts in pending:
                f = funding_at(series, times, _ts_ms(ts))
                if f is None:
                    continue
                fh.write(json.dumps({"id": oid, "symbol": symbol, "ts": ts, **f}) + "\n")
                written += 1
            if n % 25 == 0:
                log.info("funding: %d/%d symbols, %d rows", n, len(targets), written)
    stats = {"written": written, "skipped_existing": skipped, "symbols_without_perp": no_perp}
    log.info("backfill_funding: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="*")
    args = ap.parse_args(argv)
    print(run(config(), symbols=args.symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
