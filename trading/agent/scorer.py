"""Outcome scoring — the build step that fills the experience store (the RAG).

Nothing here decides anything. The scorer turns append-only records the
system already produces — every venue's decision journal and its own
observation log — into measured aggregates the decide prompt can cite:

    open   : first sighting of a symbol (universe sweep, model pick, shadow
             random pick, or a random-arm entry) with its features, its venue
             and the venue's price at that moment
    resolve: once the horizon elapses, the forward return from the venue's own
             price record (mainnet klines for Binance, the archive's parquet
             for KR/US), the max run-up and drawdown along the way, whether
             the TARGET was reached before the STOP under the live exit
             contract, and the book's benchmark over the identical window

The store is POOLED across sleeves. Features are unit-free (a flow share in
[-1, 1], a percentage change), so an observation from any venue is evidence
for every venue — labelled with where it came from, never blended silently:
every bucket renders pooled AND per venue, and the prompt for a venue sees
both.

Three measurements added 2026-09-03, each answering a defect seen live:

* **Excess return.** Live universe buckets read −2.7% while the same buckets
  in the backtest read +1.2% — regime, not signal, methodology trap #3 back
  inside the RAG. Every resolution also measures the book's benchmark over
  the same window; `avg_excess_pct` is what the prompt should weigh.
* **Robust statistics.** One +200% pump made "random +22.5%" on n=9. Buckets
  carry the median and the clearance rate beside the mean.
* **Calibration.** The model states a confidence on every pick and the 0.45
  floor is the decision boundary, yet nothing measured whether a 0.50 call
  clears its target half the time. Now it does, per confidence band.

De-overlapped per symbol: one open universe observation at a time, because
scoring every 15-minute sighting of the same rally as independent evidence is
methodology trap #2 and manufactures fake edges. Aggregates carry their sample
size, and `experience_block` refuses to render any bucket below
`score.min_bucket_n` — an unfilled store must say nothing, not guess.

Runs inside the service between cycles (`maybe_run`), or standalone:

    uv run python -m trading.agent.scorer
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import random
import statistics
import time
from pathlib import Path

from trading.agent.prices import price_source
from trading.config import AppConfig, config
from trading.risk.exits import ExitPolicy

log = logging.getLogger(__name__)

# Signed 24h-change bands for bucketing, in percent. The screen's entry band is
# 15..60; bands on both sides of it exist so the screen itself can be judged.
CHANGE_BANDS = ((None, 0.0), (0.0, 15.0), (15.0, 40.0), (40.0, None))
LIVE_SOURCES = ("model", "shadow", "random", "universe")


def _band_label(lo, hi) -> str:
    if lo is None:
        return f"<{hi:g}%"
    if hi is None:
        return f">{lo:g}%"
    return f"{lo:g}..{hi:g}%"


def _in_band(value: float, lo, hi) -> bool:
    if lo is not None and value < lo:
        return False
    return not (hi is not None and value >= hi)


def _num(value) -> float:
    """Screen rows may carry '+12.34' strings; unparseable -> 0.0."""
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", "").replace("+", ""))
    except ValueError:
        return 0.0


def _is_true(value) -> bool:
    return str(value).lower() == "true"


def _venue_of_book(book: str) -> str:
    return "BINANCE" if book in ("CRYPTO", "BSTOCKS", "BINANCE", "") else book


def confidence_band(value: float, edges: list[float]) -> str:
    lo = 0.0
    for hi in edges:
        if value < hi:
            return f"{lo:.2f}-{hi:.2f}"
        lo = hi
    return f"{lo:.2f}+"


def bootstrap_ci(
    diffs: list[float], *, samples: int, seed: int, level: float
) -> tuple[float, float] | None:
    """Percentile bootstrap interval on the mean of paired differences."""
    if len(diffs) < 2 or samples <= 0:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    tail = (1 - level) / 2
    lo = means[max(int(tail * samples), 0)]
    hi = means[min(int((1 - tail) * samples), samples - 1)]
    return round(lo, 3), round(hi, 3)


class ExperienceScorer:
    def __init__(self, client, screen, ledger, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.scfg = self.cfg.score
        self.client = client  # data-plane reads go to mainnet by construction
        self.screen = screen
        self.ledger = ledger
        self.obs_path = Path(self.scfg.observations)
        self.exp_path = Path(self.scfg.experience)
        # Every venue's journal: the store is pooled across sleeves.
        self.journal_paths = {v: self.cfg.journal_for(v) for v in self.scfg.venues}
        self.journal_path = self.journal_paths.get("BINANCE", Path(self.cfg.agent.journal))
        self._last_run = 0.0
        self._sources: dict[str, object] = {}
        self._bench_cache: dict[tuple[str, str, int], float | None] = {}

    # -- persistence ----------------------------------------------------------

    def _append(self, record: dict) -> None:
        self.obs_path.parent.mkdir(parents=True, exist_ok=True)
        with self.obs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _load(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """(opens, resolves) keyed by observation id. Idempotent re-reads."""
        opens: dict[str, dict] = {}
        resolves: dict[str, dict] = {}
        if not self.obs_path.exists():
            return opens, resolves
        with self.obs_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                target = opens if rec.get("kind") == "open" else resolves
                target[rec.get("id", "")] = rec
        return opens, resolves

    # -- one pass -------------------------------------------------------------

    def maybe_run(self) -> None:
        """Interval-gated pass; safe to call every cycle."""
        if not self.scfg.enabled:
            return
        if self._last_run and time.monotonic() - self._last_run < self.scfg.interval_minutes * 60:
            return
        self._last_run = time.monotonic()
        try:
            self.run_once()
        except Exception:
            # Scoring must never break a trading cycle.
            log.exception("scorer pass failed")

    def run_once(self) -> dict:
        opens, resolves = self._load()
        from_journal = self._open_from_journal(opens, resolves)
        from_universe = self._open_universe(opens, resolves) if self.screen is not None else 0
        resolved = self._resolve_due(opens, resolves)
        experience = self._aggregate(opens, resolves)
        self.exp_path.parent.mkdir(parents=True, exist_ok=True)
        self.exp_path.write_text(
            json.dumps(experience, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        stats = {
            "opened_journal": from_journal,
            "opened_universe": from_universe,
            "resolved": resolved,
            "buckets": len(experience.get("buckets", [])),
        }
        log.info("scorer: %s", stats)
        return stats

    # -- opening observations -------------------------------------------------

    def _open_from_journal(self, opens: dict, resolves: dict) -> int:
        """Model picks, shadow picks and random-arm entries, from EVERY venue.

        Each journal is re-read from the start every pass and observation ids
        are derived from the journalled timestamps, so this is idempotent —
        no cursor to lose, no double counting. The venue is the journal the
        record came from: that is what decides which price record resolves it.
        """
        count = 0
        for venue, path in self.journal_paths.items():
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    count += self._open_from_record(rec, venue, opens, resolves)
        return count

    def _open_from_record(self, rec: dict, venue: str, opens: dict, resolves: dict) -> int:
        count = 0
        ts = rec.get("ts", "")
        if rec.get("kind") == "decision":
            features = {c.get("symbol"): c for c in rec.get("candidates", [])}
            picked: dict[str, float | None] = {}
            for v in rec.get("verdicts", []):
                intent = v.get("intent", {})
                if str(intent.get("side", "")).upper().endswith("BUY"):
                    picked[intent["symbol"]] = intent.get("confidence")
            # The virtual pick is the model's top candidate on EVERY decision,
            # declines included — it grows the model-vs-random corpus at
            # decision rate instead of trade rate. Traded picks still open
            # below; identical ids dedupe.
            virtual = rec.get("virtual_pick")
            if virtual:
                count += self._open_pick(
                    "model",
                    virtual,
                    ts,
                    features,
                    opens,
                    resolves,
                    venue=venue,
                    confidence=rec.get("virtual_confidence"),
                )
            for symbol, conf in picked.items():
                count += self._open_pick(
                    "model", symbol, ts, features, opens, resolves, venue=venue, confidence=conf
                )
            shadow = rec.get("shadow_random")
            if shadow:
                count += self._open_pick("shadow", shadow, ts, features, opens, resolves, venue)
        elif rec.get("kind") == "explore" and rec.get("sent"):
            entry = rec.get("entry") or {}
            symbol = entry.get("symbol")
            if symbol:
                count += self._open_pick(
                    "random", symbol, ts, {symbol: entry}, opens, resolves, venue
                )
        return count

    def _open_pick(
        self,
        source: str,
        symbol: str,
        ts: str,
        features: dict,
        opens: dict,
        resolves: dict,
        venue: str = "BINANCE",
        confidence: float | None = None,
    ) -> int:
        venue = str(venue).upper()
        # Binance ids keep their historical shape so nothing already stored
        # is reopened; other venues carry the venue in the id.
        obs_id = (
            f"{source}:{symbol}:{ts}" if venue == "BINANCE" else f"{source}:{venue}:{symbol}:{ts}"
        )
        if obs_id in opens or obs_id in resolves:
            return 0
        f = features.get(symbol) or {}
        price = abs(_num(f.get("price")))
        if price <= 0:
            return 0  # no anchor price, no observation
        quote_volume = _num(f.get("quote_volume"))
        if not quote_volume:
            # Kiwoom screens report share volume; turnover is price x shares.
            quote_volume = price * _num(f.get("volume"))
        flow = f.get("taker_buy_share")
        if flow is None:
            flow = f.get("taker_share")
        rec = {
            "kind": "open",
            "id": obs_id,
            "source": source,
            "venue": venue,
            "symbol": symbol,
            "ts": ts,
            "decision_ts": ts,
            "price": price,
            "book": f.get("book") or (venue if venue != "BINANCE" else ""),
            "change_pct": _num(f.get("change_pct")),
            "quote_volume": quote_volume,
            "taker_share": flow,
        }
        if confidence is not None:
            with contextlib.suppress(TypeError, ValueError):
                rec["confidence"] = float(confidence)
        self._append(rec)
        opens[obs_id] = rec
        return 1

    def _open_universe(self, opens: dict, resolves: dict) -> int:
        """One open observation per tradable symbol at a time (Binance sweep).

        order_size 0 applies only the absolute liquidity floors, so the sweep
        covers the widest pool any order size could reach. KR/US have no live
        sweep — a sweep is API calls, and the Kiwoom token has an owner after
        hours; their universe evidence comes from the archive backfill.
        """
        active = {
            rec["symbol"]
            for oid, rec in opens.items()
            if rec.get("source") == "universe" and oid not in resolves
        }
        pool = [e for e in self.screen.tradable_pool(0.0) if e["symbol"] not in active]
        pool = pool[: self.scfg.max_opens_per_run]
        if not pool:
            return 0
        flow = self.screen._flow([e["symbol"] for e in pool])
        now = dt.datetime.now(dt.UTC).isoformat()
        count = 0
        for e in pool:
            obs_id = f"universe:{e['symbol']}:{now}"
            rec = {
                "kind": "open",
                "id": obs_id,
                "source": "universe",
                "venue": "BINANCE",
                "symbol": e["symbol"],
                "ts": now,
                "price": e["price"],
                "book": e["book"],
                "change_pct": e["change_pct"],
                "quote_volume": e["quote_volume"],
                "taker_share": flow.get(e["symbol"]),
            }
            self._append(rec)
            opens[obs_id] = rec
            count += 1
        return count

    # -- resolving ------------------------------------------------------------

    def _source(self, venue: str):
        if venue not in self._sources:
            self._sources[venue] = price_source(venue, self.cfg, self.client)
        return self._sources[venue]

    def horizon_for(self, venue: str) -> dt.timedelta:
        """The venue's own hold window — an observation is graded on the
        horizon its exit contract would actually have held it for."""
        minutes = self.cfg.exits.for_market(venue).max_hold_minutes
        if str(venue).upper() == "BINANCE" or not minutes:
            minutes = self.scfg.horizon_minutes
        return dt.timedelta(minutes=minutes)

    def _resolve_due(self, opens: dict, resolves: dict) -> int:
        now = dt.datetime.now(dt.UTC)
        grace = dt.timedelta(minutes=self.scfg.resolve_grace_minutes)
        count = 0
        for obs_id, rec in opens.items():
            if obs_id in resolves:
                continue
            try:
                opened = dt.datetime.fromisoformat(rec["ts"])
            except (KeyError, ValueError):
                continue
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=dt.UTC)
            venue = str(rec.get("venue") or _venue_of_book(rec.get("book") or "")).upper()
            horizon = self.horizon_for(venue)
            if now - opened < horizon:
                continue
            resolved = self._resolve_one(rec, opened, horizon, venue, now, grace)
            if resolved is not None:
                self._append(resolved)
                resolves[obs_id] = resolved
                count += 1
        return count

    def _benchmark_return(
        self, venue: str, book: str, opened: dt.datetime, horizon: dt.timedelta
    ) -> float | None:
        symbol = self.scfg.benchmarks.get(book) or self.scfg.benchmarks.get(venue)
        if not symbol:
            return None
        key = (venue, symbol, int(opened.timestamp() // 3600))
        if key in self._bench_cache:
            return self._bench_cache[key]
        source = self._source(venue)
        window = source.window(symbol, opened, opened + horizon) if source else None
        value = None
        if window and window.bars:
            first, last = window.bars[0].open, window.bars[-1].close
            if first > 0:
                value = round((last / first - 1) * 100, 4)
        self._bench_cache[key] = value
        return value

    def _resolve_one(
        self,
        rec: dict,
        opened: dt.datetime,
        horizon: dt.timedelta,
        venue: str,
        now: dt.datetime,
        grace: dt.timedelta,
    ) -> dict | None:
        entry = float(rec.get("price") or 0)
        if entry <= 0:
            return None
        source = self._source(venue)
        if source is None:
            return None
        window = source.window(rec["symbol"], opened, opened + horizon)
        if window is None or not window.bars:
            return None
        if not window.complete and now < opened + horizon + grace:
            return None  # the price record has not caught up yet; wait
        end_price = window.end_price
        book = rec.get("book") or venue
        hurdle = self.ledger.breakeven_move_pct(book if book else "BINANCE")
        forward = end_price / entry - 1
        # The live exit contract's levels, from the SAME arithmetic the
        # supervisor runs, so "reached the target before the stop" is graded
        # against what the position would actually have been held to.
        plan = ExitPolicy(self.cfg, self.ledger, market=venue).plan_for("obs", entry, 1.0)
        target_pct = plan.target / entry - 1
        stop_pct = 1 - plan.stop / entry
        outcome = window.target_before_stop(entry, target_pct, stop_pct)
        bench = self._benchmark_return(venue, str(book), opened, horizon)
        forward_pct = round(forward * 100, 4)
        return {
            "kind": "resolve",
            "id": rec["id"],
            "ts": now.isoformat(),
            "end_price": end_price,
            "forward_return_pct": forward_pct,
            "max_runup_pct": round(window.runup_pct(entry), 4),
            "max_drawdown_pct": round(window.drawdown_pct(entry), 4),
            "hurdle_pct": round(hurdle * 100, 4),
            "cleared_hurdle": forward > hurdle,
            "target_pct": round(target_pct * 100, 4),
            "stop_pct": round(stop_pct * 100, 4),
            "outcome": outcome,
            "cleared_target": outcome == "target",
            "benchmark_return_pct": bench,
            "excess_return_pct": round(forward_pct - bench, 4) if bench is not None else None,
            "bars": len(window.bars),
            "interval": window.interval,
        }

    # -- closed trades ---------------------------------------------------------

    def _trade_outcomes(self) -> list[dict]:
        """Closed round trips graded against each market's hurdle.

        The FIFO pairing itself lives in `CostLedger.closed_trades` — the
        ledger owns its records, and the bot's P&L section walks the same code,
        so the two can never disagree about what a round trip was.
        """
        outcomes = []
        for t in self.ledger.closed_trades(
            since=self.scfg.trade_since, markets=set(self.scfg.trade_markets)
        ):
            hurdle = self.ledger.breakeven_move_pct(t["market"] or "BINANCE")
            outcomes.append(
                {
                    "symbol": t["symbol"],
                    "forward_return_pct": round(t["return_pct"], 4),
                    "cleared_hurdle": t["return_pct"] / 100 > hurdle,
                }
            )
        return outcomes

    # -- aggregation ----------------------------------------------------------

    @staticmethod
    def _bucket(label: str, members: list[dict]) -> dict:
        n = len(members)
        returns = [float(m.get("forward_return_pct", 0) or 0) for m in members]
        cleared = sum(1 for m in members if _is_true(m.get("cleared_hurdle")))
        excess = [
            float(m["excess_return_pct"]) for m in members if m.get("excess_return_pct") is not None
        ]
        targeted = [m for m in members if m.get("cleared_target") is not None]
        return {
            "label": label,
            "n": n,
            "cleared": cleared,
            "clear_rate": round(cleared / n, 3) if n else None,
            "avg_return_pct": round(sum(returns) / n, 3) if n else 0.0,
            "median_return_pct": round(statistics.median(returns), 3) if n else 0.0,
            "n_excess": len(excess),
            "avg_excess_pct": round(sum(excess) / len(excess), 3) if excess else None,
            "n_target": len(targeted),
            "cleared_target": sum(1 for m in targeted if _is_true(m.get("cleared_target"))),
        }

    def _pairs(self, model: list[dict], shadow: list[dict]) -> dict:
        """Paired model-vs-shadow: same decision, same venue, both resolved."""

        def key(r):
            return (str(r.get("venue") or "BINANCE"), r.get("decision_ts"))

        m = {key(r): r for r in model}
        s = {key(r): r for r in shadow}
        pairs = [
            (float(m[k]["forward_return_pct"]), float(s[k]["forward_return_pct"]))
            for k in m.keys() & s.keys()
        ]
        diffs = [a - b for a, b in pairs]
        n = len(pairs)
        ci = bootstrap_ci(
            diffs,
            samples=self.scfg.bootstrap_samples,
            seed=self.scfg.bootstrap_seed,
            level=self.scfg.ci_level,
        )
        return {
            "n": n,
            "model_avg_pct": round(sum(p[0] for p in pairs) / n, 3) if n else None,
            "shadow_avg_pct": round(sum(p[1] for p in pairs) / n, 3) if n else None,
            "mean_diff_pct": round(sum(diffs) / n, 3) if n else None,
            "median_diff_pct": round(statistics.median(diffs), 3) if n else None,
            "model_wins": sum(1 for d in diffs if d > 0),
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
            "ci_level": self.scfg.ci_level,
        }

    def _calibration(self, model_rows: list[dict]) -> list[dict]:
        """Stated confidence vs realised target-before-stop, per band."""
        graded = [
            r
            for r in model_rows
            if r.get("confidence") is not None and r.get("cleared_target") is not None
        ]
        by_band: dict[tuple[str, str], list[dict]] = {}
        for r in graded:
            band = confidence_band(float(r["confidence"]), self.scfg.confidence_bands)
            by_band.setdefault((band, ""), []).append(r)
            by_band.setdefault((band, str(r.get("venue") or "BINANCE")), []).append(r)
        out = []
        for (band, venue), members in sorted(by_band.items()):
            hits = sum(1 for r in members if _is_true(r.get("cleared_target")))
            n = len(members)
            out.append(
                {
                    "band": band,
                    "venue": venue,
                    "n": n,
                    "hits": hits,
                    "hit_rate": round(hits / n, 3),
                    "stated": round(sum(float(r["confidence"]) for r in members) / n, 3),
                    "avg_return_pct": round(
                        sum(float(r.get("forward_return_pct", 0)) for r in members) / n, 3
                    ),
                }
            )
        return out

    def _aggregate(self, opens: dict, resolves: dict) -> dict:
        rows = []
        for obs_id, res in resolves.items():
            open_rec = opens.get(obs_id)
            if open_rec:
                rows.append({**open_rec, **res})
        for r in rows:
            r.setdefault("venue", _venue_of_book(r.get("book") or ""))

        buckets = []
        by_source: dict[str, list[dict]] = {}
        for r in rows:
            by_source.setdefault(r.get("source", "?"), []).append(r)
        for source, members in sorted(by_source.items()):
            buckets.append(self._bucket(f"{source} picks", members))
            # Per venue too, whenever a live source spans more than one:
            # pooled evidence informs every sleeve, labelled evidence lets
            # the operator see which sleeve earned it.
            venues = sorted({r.get("venue") for r in members})
            if source in LIVE_SOURCES and len(venues) > 1:
                for venue in venues:
                    buckets.append(
                        self._bucket(
                            f"{source} picks:{venue}", [r for r in members if r["venue"] == venue]
                        )
                    )

        # Same band/tertile structure for the live universe sweep and the
        # backtest replay — but NEVER pooled: provenance stays in the label, so
        # the prompt and the operator always see which numbers were waited for
        # and which were reconstructed from history.
        for prefix in ("universe", "backtest", "backtest_kr", "backtest_us"):
            members_all = by_source.get(prefix, [])
            if prefix != "universe" and not members_all:
                continue
            by_book: dict[str, list[dict]] = {}
            for r in members_all:
                by_book.setdefault(r.get("book") or "?", []).append(r)
            for book, members in sorted(by_book.items()):
                buckets.append(self._bucket(f"{prefix}:{book}", members))

            for lo, hi in CHANGE_BANDS:
                members = [
                    r for r in members_all if _in_band(float(r.get("change_pct") or 0), lo, hi)
                ]
                buckets.append(self._bucket(f"{prefix} 24h-change {_band_label(lo, hi)}", members))

            flowed = sorted(
                (r for r in members_all if r.get("taker_share") is not None),
                key=lambda r: r["taker_share"],
            )
            if flowed:
                k = max(len(flowed) // 3, 1)
                for label, members in (
                    ("flow tertile low", flowed[:k]),
                    ("flow tertile high", flowed[-k:]),
                ):
                    buckets.append(self._bucket(f"{prefix} {label}", members))

        closed = self._trade_outcomes()
        if closed:
            buckets.append(self._bucket("closed trades (mark-to-mainnet)", closed))

        model_rows = by_source.get("model", [])
        shadow_rows = by_source.get("shadow", [])
        pair_summary = self._pairs(model_rows, shadow_rows)
        pairs_by_venue = {}
        for venue in sorted(
            {r.get("venue") for r in model_rows} | {r.get("venue") for r in shadow_rows}
        ):
            pv = self._pairs(
                [r for r in model_rows if r.get("venue") == venue],
                [r for r in shadow_rows if r.get("venue") == venue],
            )
            if pv["n"]:
                pairs_by_venue[venue] = pv

        return {
            "meta": {
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                "horizon_minutes": self.scfg.horizon_minutes,
                "horizons": {
                    v: int(self.horizon_for(v).total_seconds() // 60) for v in self.scfg.venues
                },
                "min_bucket_n": self.scfg.min_bucket_n,
                "resolved_observations": len(rows),
                "benchmarks": dict(self.scfg.benchmarks),
            },
            "buckets": buckets,
            "model_vs_shadow": pair_summary,
            "model_vs_shadow_by_venue": pairs_by_venue,
            "calibration": self._calibration(model_rows),
        }


def _render_bucket(b: dict, min_n: int) -> str:
    text = f"{b['cleared']}/{b['n']} cleared hurdle"
    if b.get("clear_rate") is not None:
        text += f" ({b['clear_rate']:.0%})"
    text += f", avg {b['avg_return_pct']:+.2f}%"
    if b.get("median_return_pct") is not None:
        text += f", median {b['median_return_pct']:+.2f}%"
    if b.get("avg_excess_pct") is not None and b.get("n_excess", 0) >= min_n:
        text += f", {b['avg_excess_pct']:+.2f}% vs benchmark (n={b['n_excess']})"
    return text


def _render_pairs(p: dict) -> str:
    text = f"n={p['n']}: model {p['model_avg_pct']:+.2f}% vs random {p['shadow_avg_pct']:+.2f}%"
    if p.get("mean_diff_pct") is not None:
        text += f", paired diff {p['mean_diff_pct']:+.2f}%"
        if p.get("ci_low") is not None:
            text += f" ({p.get('ci_level', 0.95):.0%} CI {p['ci_low']:+.2f}..{p['ci_high']:+.2f})"
        text += f", model wins {p.get('model_wins', 0)}/{p['n']}"
    return text


def experience_block(cfg: AppConfig | None = None, venue: str | None = None) -> dict | None:
    """The prompt-side read: qualifying aggregates only, or None.

    An unfilled store must say nothing — rendering a half-empty record would
    hand the model confident-sounding noise. Every rendered row carries its n.
    Pooled rows always render; per-venue rows render only for `venue`, so a
    KR prompt sees the pooled evidence plus KR's own, never US's.
    """
    cfg = cfg or config()
    path = Path(cfg.score.experience)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    min_n = cfg.score.min_bucket_n
    venue = str(venue).upper() if venue else None
    rows = {}
    for b in data.get("buckets", []):
        if b.get("n", 0) < min_n:
            continue
        label = b["label"]
        # "<source> picks:<venue>" is a per-venue split of a live source;
        # "backtest_kr:KR" is a book split and renders as it is.
        if label.startswith(tuple(f"{src} picks:" for src in LIVE_SOURCES)):
            if label.split(":", 1)[1] != venue:
                continue
            label = f"{label.split(':', 1)[0]} (this venue)"
        rows[label] = _render_bucket(b, min_n)
    pairs = data.get("model_vs_shadow", {})
    if pairs.get("n", 0) >= min_n:
        rows["model vs random (paired)"] = _render_pairs(pairs)
    venue_pairs = (data.get("model_vs_shadow_by_venue") or {}).get(venue or "", {})
    if venue_pairs.get("n", 0) >= min_n:
        rows["model vs random (paired, this venue)"] = _render_pairs(venue_pairs)
    calibration = {}
    for c in data.get("calibration", []):
        if c.get("n", 0) < min_n:
            continue
        if c.get("venue") and c["venue"] != venue:
            continue
        label = f"confidence {c['band']}" + (" (this venue)" if c.get("venue") else "")
        calibration[label] = (
            f"{c['hits']}/{c['n']} reached target before stop ({c['hit_rate']:.0%}) "
            f"against a stated {c['stated']:.2f}; avg {c['avg_return_pct']:+.2f}%"
        )
    if not rows and not calibration:
        return None
    horizon = data.get("meta", {}).get("horizon_minutes")
    block = {
        "note": (
            f"Measured on this system's own forward returns at each venue's hold horizon "
            f"({horizon}-minute on Binance), from the venue's own price record. Pooled "
            "rows span every venue; '(this venue)' rows are this market alone. "
            "'vs benchmark' is the excess over the book's benchmark across the same "
            "window. Small samples are withheld."
        ),
        "record": rows,
    }
    if calibration:
        block["your_calibration"] = calibration
        block["calibration_note"] = (
            "Your past stated confidences against what actually happened. If a band "
            "hits below its stated probability, you are overconfident there."
        )
    return block


def main() -> int:
    import argparse

    from trading.brokers.adapters import build_adapter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="binance", choices=["binance"])
    ap.add_argument("--venue", default=None, help="render the prompt block for this venue")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    adapter = build_adapter(args.broker, None, cfg)
    from trading.accounting.costs import CostLedger

    scorer = ExperienceScorer(adapter.client, adapter.screen, CostLedger(cfg), cfg)
    stats = scorer.run_once()
    print(json.dumps(stats, indent=1))
    block = experience_block(cfg, venue=args.venue)
    print(
        json.dumps(block, ensure_ascii=False, indent=1) if block else "(no qualifying buckets yet)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
