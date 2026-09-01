"""Backfill the experience store from historical mainnet klines.

    uv run python -m trading.agent.backfill            # score.backfill_days of history
    uv run python -m trading.agent.backfill --days 30

The live scorer must wait 72 hours for every observation to resolve; for a
HISTORICAL timestamp the future already exists in the klines, so an observation
opened in the past resolves immediately. One pass fills the change-band and
flow-tertile buckets with thousands of samples instead of waiting weeks.

Provenance is never mixed: everything written here carries ``source:
"backtest"`` and aggregates into ``backtest``-labelled buckets, separate from
the live-measured ones. The three methodology traps are enforced by
construction: observations are stepped one horizon apart per symbol (no
overlap), every window that has enough bars resolves (none dropped), and the
aggregate is judged next to the same-period universe average (the benchmark is
the bucket structure itself).

Known bias, accepted and documented: the pool is TODAY's tradable universe, so
symbols that delisted during the window are missing — survivorship. It biases
the backtest optimistic; live buckets remain the confirming measurement.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

MS_PER_HOUR = 3_600_000
FLOW_HOURS = 120  # mirror the live 5-day flow lookback, in hourly bars


def fetch_hourly(client, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """All 1h klines in [start_ms, end_ms], paginated past the 1000-bar limit."""
    bars: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        rows = client.call(
            "klines",
            {"symbol": symbol, "interval": "1h", "startTime": cursor, "limit": 1000},
        ).body.get("rows", [])
        rows = [r for r in rows if len(r) > 9 and int(r[0]) < end_ms]
        if not rows:
            break
        bars.extend(rows)
        cursor = int(rows[-1][0]) + MS_PER_HOUR
        if len(rows) < 1000:
            break
    return bars


def features_at(bars: list[list], i: int) -> dict | None:
    """Screen-equivalent features at bar index i, from bars[: i + 1] ONLY.

    Lookahead is the deadliest backtest bug, so the slice is structural: nothing
    after index i is touched.
    """
    if i < 24:
        return None  # not enough history for a 24h change
    window = bars[: i + 1]
    price = float(window[-1][4])
    ref = float(window[-25][4])
    if price <= 0 or ref <= 0:
        return None
    flow_bars = window[-FLOW_HOURS:]
    shares = [float(b[9]) / float(b[5]) for b in flow_bars if float(b[5]) > 0]
    return {
        "price": price,
        "change_pct": round((price / ref - 1) * 100, 3),
        "quote_volume": round(sum(float(b[7]) for b in window[-24:]), 2),
        "taker_share": round(sum(shares) / len(shares), 4) if shares else None,
    }


def resolve_forward(bars: list[list], i: int, horizon_hours: int, entry: float) -> dict | None:
    """Forward return over bars (i, i + horizon]; None if the window is short."""
    window = bars[i + 1 : i + 1 + horizon_hours]
    if len(window) < horizon_hours:
        return None
    end_price = float(window[-1][4])
    highs = [float(b[2]) for b in window]
    lows = [float(b[3]) for b in window]
    return {
        "end_price": end_price,
        "forward_return_pct": round((end_price / entry - 1) * 100, 4),
        "max_runup_pct": round((max(highs) / entry - 1) * 100, 4),
        "max_drawdown_pct": round((min(lows) / entry - 1) * 100, 4),
    }


class Backfiller:
    def __init__(self, client, screen, ledger: CostLedger, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.scfg = self.cfg.score
        self.client = client  # data plane: mainnet by construction
        self.screen = screen
        self.ledger = ledger
        self.obs_path = Path(self.scfg.observations)

    def _existing_ids(self) -> set[str]:
        if not self.obs_path.exists():
            return set()
        ids = set()
        with self.obs_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    ids.add(json.loads(line).get("id", ""))
                except json.JSONDecodeError:
                    continue
        return ids

    def run(self, days: int | None = None) -> dict:
        days = days or self.scfg.backfill_days
        horizon_h = max(int(self.scfg.horizon_minutes // 60), 1)
        # One horizon between observations per symbol: methodology trap #2
        # (overlapping windows) is excluded by construction, not by discipline.
        step = horizon_h
        end = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        start_ms = int((end - dt.timedelta(days=days)).timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        seen = self._existing_ids()
        pool = self.screen.tradable_pool(0.0)
        opened = skipped = 0
        with self.obs_path.open("a", encoding="utf-8") as fh:
            for entry in pool:
                symbol, book = entry["symbol"], entry["book"]
                try:
                    bars = fetch_hourly(self.client, symbol, start_ms, end_ms)
                except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
                    log.warning("backfill %s failed: %s", symbol, exc)
                    continue
                hurdle = self.ledger.breakeven_move_pct(book or "BINANCE")
                for i in range(FLOW_HOURS, len(bars) - horizon_h, step):
                    ts = dt.datetime.fromtimestamp(int(bars[i][0]) / 1000, dt.UTC).isoformat()
                    obs_id = f"backtest:{symbol}:{ts}"
                    if obs_id in seen:
                        skipped += 1
                        continue
                    f = features_at(bars, i)
                    fwd = resolve_forward(bars, i, horizon_h, f["price"]) if f else None
                    if f is None or fwd is None:
                        continue
                    common = {"id": obs_id, "source": "backtest", "symbol": symbol, "ts": ts}
                    fh.write(
                        json.dumps(
                            {"kind": "open", **common, "book": book, **f}, ensure_ascii=False
                        )
                        + "\n"
                    )
                    fh.write(
                        json.dumps(
                            {
                                "kind": "resolve",
                                "id": obs_id,
                                "ts": ts,
                                **fwd,
                                "hurdle_pct": round(hurdle * 100, 4),
                                "cleared_hurdle": fwd["forward_return_pct"] / 100 > hurdle,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    seen.add(obs_id)
                    opened += 1
        stats = {"opened": opened, "skipped_existing": skipped, "symbols": len(pool)}
        log.info("backfill: %s", stats)
        return stats


def main() -> int:
    import argparse

    from trading.brokers.adapters import build_adapter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="binance", choices=["binance"])
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    adapter = build_adapter(args.broker, None, cfg)
    ledger = CostLedger(cfg)
    stats = Backfiller(adapter.client, adapter.screen, ledger, cfg).run(days=args.days)
    print(json.dumps(stats, indent=1))

    # Rebuild the experience store so the new buckets render immediately.
    from trading.agent.scorer import ExperienceScorer

    ExperienceScorer(adapter.client, adapter.screen, ledger, cfg).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
