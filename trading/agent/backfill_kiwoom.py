"""Backfill the experience store for KR/US from the ai-trading-history archive.

    uv run python -m trading.agent.backfill_kiwoom --market KR
    uv run python -m trading.agent.backfill_kiwoom --market US --days 180

Reads the sibling archive's parquet klines (daily bars, KR back to 2020) and,
for KR, its per-symbol investor net-flow files — so the RAG fills with years of
KR/US regime evidence in one offline pass: **zero Kiwoom API calls, zero token
risk to the archive's downloader** (which owns the single OAuth token and works
only while markets are closed).

The KR flow feature answers the 2026-08-10 research question directly: the
`taker_share` field carries a unit-free **investor flow share** — the 5-day net
(foreigner + institution) over total absolute investor activity, in [-1, 1] —
so the existing flow-tertile buckets measure whether 외국인/기관 net buying
predicts forward returns the way taker flow does on Binance. Unit-free by
construction because the archive's 금액/수량 unit choice is not recorded.

Same integrity rules as the Binance backfill, enforced structurally: features
from slices bounded at the decision bar (no lookahead), one horizon between
observations per symbol (no overlap), `backtest_kr`/`backtest_us` provenance
never pooled with live buckets. Survivorship note: the archive KEEPS delisted
symbols' history, so this backfill is less survivorship-biased than the
Binance one — the symbol cap by recent turnover is the remaining tilt.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

FLOW_DAYS = 5  # mirror the Binance 5-day flow lookback
MIN_HISTORY_BARS = 21  # need a prior bar for change + a flow window


def _load_bars(path: Path) -> list[dict]:
    import pyarrow.parquet as pq

    rows = pq.read_table(path).to_pylist()
    rows.sort(key=lambda r: r["open_time"])
    return rows


def _load_flow(path: Path) -> dict[str, tuple[float, float]]:
    """date-iso -> (net foreigner+institution, total absolute activity)."""
    import pyarrow.parquet as pq

    out: dict[str, tuple[float, float]] = {}
    for r in pq.read_table(path).to_pylist():
        d = r.get("date")
        if d is None:
            continue
        key = d.date().isoformat() if isinstance(d, dt.datetime) else str(d)[:10]
        ind = float(r.get("ind_invsr") or 0)
        frg = float(r.get("frgnr_invsr") or 0)
        org = float(r.get("orgn") or 0)
        out[key] = (frg + org, abs(ind) + abs(frg) + abs(org))
    return out


def flow_share(flow: dict, dates: list[str]) -> float | None:
    """Unit-free net-buy share over the window; None when the window is empty."""
    rows = [flow[d] for d in dates if d in flow]
    if not rows:
        return None
    denom = sum(r[1] for r in rows)
    return round(sum(r[0] for r in rows) / denom, 4) if denom > 0 else None


class KiwoomBackfiller:
    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.scfg = self.cfg.score
        self.archive = Path(self.scfg.kiwoom_archive)
        self.ledger = CostLedger(self.cfg)
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

    def _rank_symbols(self, kline_dir: Path, limit: int) -> list[Path]:
        """Deepest names by recent turnover, so a cap keeps the liquid market."""
        scored = []
        for p in sorted(kline_dir.glob("*.parquet")):
            try:
                bars = _load_bars(p)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stall the rest
                log.warning("unreadable %s: %s", p.name, exc)
                continue
            recent = bars[-30:]
            turnover = sum(float(b["close"]) * float(b["volume"]) for b in recent)
            if len(bars) >= MIN_HISTORY_BARS and turnover > 0:
                scored.append((turnover, p, bars))
        scored.sort(key=lambda t: -t[0])
        self._bars_cache = {p: bars for _, p, bars in scored[:limit]}
        return [p for _, p, _ in scored[:limit]]

    def run(self, market: str = "KR", days: int | None = None, symbols: int = 300) -> dict:
        market = market.upper()
        venue_dir = "kiwoom_kr" if market == "KR" else "kiwoom_us"
        kline_dir = self.archive / "klines" / venue_dir / "1d"
        flow_dir = self.archive / "investor_flow" / venue_dir
        if not kline_dir.exists():
            return {"opened": 0, "note": f"no archive at {kline_dir}"}
        days = days or self.scfg.backfill_days
        # Daily bars: the horizon is expressed in TRADING DAYS. 4320 minutes is
        # three calendar days; three daily bars is the closest daily-bar
        # equivalent and keeps KR/US comparable with the Binance buckets.
        horizon_bars = max(round(self.scfg.horizon_minutes / 1440), 1)
        hurdle = self.ledger.breakeven_move_pct(market)
        cutoff_ms = int((dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).timestamp() * 1000)
        source = f"backtest_{market.lower()}"

        seen = self._existing_ids()
        opened = skipped = 0
        with self.obs_path.open("a", encoding="utf-8") as fh:
            for path in self._rank_symbols(kline_dir, symbols):
                symbol = path.stem
                bars = self._bars_cache[path]
                flow_path = flow_dir / f"{symbol}.parquet"
                flow = _load_flow(flow_path) if flow_path.exists() else {}
                start = next(
                    (i for i, b in enumerate(bars) if int(b["open_time"]) >= cutoff_ms),
                    len(bars),
                )
                first = max(start, MIN_HISTORY_BARS)
                for i in range(first, len(bars) - horizon_bars, horizon_bars):
                    window = bars[: i + 1]  # structurally no lookahead
                    price = float(window[-1]["close"])
                    prev = float(window[-2]["close"])
                    if price <= 0 or prev <= 0:
                        continue
                    date = dt.datetime.fromtimestamp(
                        int(window[-1]["open_time"]) / 1000, dt.UTC
                    ).date()
                    obs_id = f"{source}:{symbol}:{date.isoformat()}"
                    if obs_id in seen:
                        skipped += 1
                        continue
                    fdates = [
                        dt.datetime.fromtimestamp(int(b["open_time"]) / 1000, dt.UTC)
                        .date()
                        .isoformat()
                        for b in window[-FLOW_DAYS:]
                    ]
                    fwd = bars[i + 1 : i + 1 + horizon_bars]
                    end_price = float(fwd[-1]["close"])
                    highs = [float(b["high"]) for b in fwd]
                    lows = [float(b["low"]) for b in fwd]
                    forward = end_price / price - 1
                    common = {
                        "id": obs_id,
                        "source": source,
                        "symbol": symbol,
                        "ts": f"{date.isoformat()}T00:00:00+00:00",
                    }
                    fh.write(
                        json.dumps(
                            {
                                "kind": "open",
                                **common,
                                "book": market,
                                "price": price,
                                "change_pct": round((price / prev - 1) * 100, 3),
                                "quote_volume": round(price * float(window[-1]["volume"]), 2),
                                # KR: investor net-buy share; the venue's flow signal.
                                "taker_share": flow_share(flow, fdates),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    fh.write(
                        json.dumps(
                            {
                                "kind": "resolve",
                                "id": obs_id,
                                "ts": common["ts"],
                                "end_price": end_price,
                                "forward_return_pct": round(forward * 100, 4),
                                "max_runup_pct": round((max(highs) / price - 1) * 100, 4),
                                "max_drawdown_pct": round((min(lows) / price - 1) * 100, 4),
                                "hurdle_pct": round(hurdle * 100, 4),
                                "cleared_hurdle": forward > hurdle,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    seen.add(obs_id)
                    opened += 1
        stats = {"opened": opened, "skipped_existing": skipped, "market": market}
        log.info("kiwoom backfill: %s", stats)
        return stats


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", default="KR", choices=["KR", "US"])
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--symbols", type=int, default=300, help="deepest N by recent turnover")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    stats = KiwoomBackfiller(cfg).run(market=args.market, days=args.days, symbols=args.symbols)
    print(json.dumps(stats, indent=1))

    # Rebuild the experience store offline: aggregation needs no venue client.
    from trading.agent.scorer import ExperienceScorer

    scorer = ExperienceScorer(None, None, CostLedger(cfg), cfg)
    opens, resolves = scorer._load()
    experience = scorer._aggregate(opens, resolves)
    Path(cfg.score.experience).write_text(
        json.dumps(experience, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    qualifying = [b for b in experience["buckets"] if b["n"] >= cfg.score.min_bucket_n]
    for b in qualifying:
        if b["label"].startswith(f"backtest_{args.market.lower()}"):
            print(
                f"{b['label']}: n={b['n']} cleared {b['cleared']} avg {b['avg_return_pct']:+.3f}%"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
