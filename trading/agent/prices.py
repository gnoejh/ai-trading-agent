"""Per-venue price sources for resolving observations and replaying exits.

Every venue's evidence resolves against that venue's OWN price record:

* **Binance** — mainnet 1h klines through the data-plane client (testnet fills
  are fantasy prices; the plane split makes every unsigned read mainnet).
* **Kiwoom KR / US** — the ai-trading-history archive's parquet bars, read
  offline. Its downloader owns the single Kiwoom OAuth token, so this module
  must never touch a Kiwoom endpoint: an after-hours call would revoke the
  downloader's token mid-run.

Both answer the same question — the bars inside ``[start, end)`` and whether
the window is COMPLETE (a bar exists at or past ``end``). Completeness matters
because a window resolved from a half-downloaded file would grade a 2-day
horizon on one afternoon of bars.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE_DIRS = {"KR": "kiwoom_kr", "US": "kiwoom_us"}


@dataclass(slots=True)
class Bar:
    t: int  # open time, ms since epoch (UTC)
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class Window:
    bars: list[Bar] = field(default_factory=list)
    complete: bool = False
    interval: str = ""

    @property
    def end_price(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def runup_pct(self, entry: float) -> float:
        return (max(b.high for b in self.bars) / entry - 1) * 100 if self.bars else 0.0

    def drawdown_pct(self, entry: float) -> float:
        return (min(b.low for b in self.bars) / entry - 1) * 100 if self.bars else 0.0

    def target_before_stop(self, entry: float, target_pct: float, stop_pct: float) -> str:
        """Walk the bars in order: the first level touched decides.

        A bar that touches BOTH levels counts as a stop — the conservative
        reading, and the one the live supervisor would most often produce
        (a stop is checked before a target on every pass). Returns
        ``"target"``, ``"stop"`` or ``"time"`` (neither touched).
        """
        target = entry * (1 + target_pct)
        stop = entry * (1 - stop_pct)
        for b in self.bars:
            if b.low <= stop:
                return "stop"
            if b.high >= target:
                return "target"
        return "time"


def _ms(when: dt.datetime) -> int:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return int(when.timestamp() * 1000)


class BinancePriceSource:
    """Mainnet 1h klines via the data-plane client."""

    interval = "1h"

    def __init__(self, client):
        self.client = client

    def window(self, symbol: str, start: dt.datetime, end: dt.datetime) -> Window | None:
        start_ms, end_ms = _ms(start), _ms(end)
        hours = max(int((end_ms - start_ms) // 3_600_000), 1)
        try:
            rows = self.client.call(
                "klines",
                {
                    "symbol": symbol,
                    "interval": self.interval,
                    "startTime": start_ms,
                    # +2: one bar past the window proves it is complete.
                    "limit": min(hours + 2, 1000),
                },
            ).body.get("rows", [])
        except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
            log.warning("klines %s failed: %s", symbol, exc)
            return None
        bars = [
            Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in rows
            if len(r) > 4
        ]
        inside = [b for b in bars if start_ms <= b.t < end_ms]
        complete = any(b.t >= end_ms for b in bars)
        return Window(bars=inside, complete=complete, interval=self.interval)


class ArchivePriceSource:
    """Parquet bars from the ai-trading-history archive (read-only, offline).

    Intervals are tried in order: the hourly file when it already covers the
    window, otherwise the daily one — the downloader refreshes daily bars
    nightly and hourly bars with a lag, and a resolution must not wait on the
    slower feed when the faster answer is already on disk.
    """

    def __init__(self, root: str | Path, market: str, intervals: list[str] | None = None):
        self.root = Path(root)
        self.market = str(market).upper()
        self.intervals = list(intervals or ["1h", "1d"])
        self._cache: dict[tuple[str, str], tuple[float, list[Bar]]] = {}

    def _path(self, interval: str, symbol: str) -> Path:
        venue_dir = ARCHIVE_DIRS.get(self.market, f"kiwoom_{self.market.lower()}")
        return self.root / "klines" / venue_dir / interval / f"{symbol}.parquet"

    def _bars(self, interval: str, symbol: str) -> list[Bar]:
        path = self._path(interval, symbol)
        if not path.exists():
            return []
        mtime = path.stat().st_mtime
        cached = self._cache.get((interval, symbol))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            import pyarrow.parquet as pq

            rows = pq.read_table(path).to_pylist()
        except Exception as exc:  # noqa: BLE001 - a file mid-rewrite by the downloader
            log.warning("archive %s unreadable: %s", path.name, exc)
            return []
        bars = sorted(
            (
                Bar(
                    int(r["open_time"]),
                    float(r["open"]),
                    float(r["high"]),
                    float(r["low"]),
                    float(r["close"]),
                )
                for r in rows
                if r.get("open_time") is not None
            ),
            key=lambda b: b.t,
        )
        self._cache[(interval, symbol)] = (mtime, bars)
        return bars

    def window(self, symbol: str, start: dt.datetime, end: dt.datetime) -> Window | None:
        start_ms, end_ms = _ms(start), _ms(end)
        best: Window | None = None
        for interval in self.intervals:
            bars = self._bars(interval, symbol)
            if not bars:
                continue
            inside = [b for b in bars if start_ms <= b.t < end_ms]
            win = Window(bars=inside, complete=any(b.t >= end_ms for b in bars), interval=interval)
            if win.complete and inside:
                return win
            if best is None or len(inside) > len(best.bars):
                best = win
        return best


def price_source(venue: str, cfg, client=None):
    """The venue's own price record: Binance klines, or the archive for KR/US."""
    venue = str(venue).upper()
    if venue == "BINANCE" or venue in ("CRYPTO", "BSTOCKS"):
        return BinancePriceSource(client) if client is not None else None
    return ArchivePriceSource(cfg.score.kiwoom_archive, venue, cfg.score.archive_intervals)
