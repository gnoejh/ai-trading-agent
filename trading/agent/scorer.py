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
ARM_PREFIX = "arm_"


def _feat(c: dict, key: str) -> float | None:
    v = c.get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pick_by(menu: list[dict], key: str, *, largest: bool) -> str | None:
    """The symbol with the extreme value of one feature; None if none carry it.

    Ties break on symbol so the pick is deterministic across scorer runs --
    a re-run must reopen the SAME observation id, not a new one.
    """
    rows = []
    for c in menu:
        v = _feat(c, key)
        if v is not None:
            rows.append((v, str(c.get("symbol"))))
    if not rows:
        return None
    return (max(rows) if largest else min(rows))[1]


# Deterministic selector arms: each a pure function of the journalled MENU.
# They exist so the LLM is not the only selector on trial. Every arm is scored
# against the same shadow on the same menus, in the same paired test, so the
# leaderboard they produce is a like-for-like answer to "does ANY selection
# rule here beat a random draw from the menu". The arms cost nothing: no slot,
# no dollar, no change to the decide path -- and because the menu is on every
# decision record, they are RETROACTIVE over the whole journalled epoch.
SELECTORS: dict[str, object] = {
    "flow_top": lambda menu: _pick_by(menu, "taker_buy_share", largest=True),
    "prior_top": lambda menu: _pick_by(menu, "p_clear", largest=True),
    "volume_top": lambda menu: _pick_by(menu, "quote_volume", largest=True),
    "change_low": lambda menu: _pick_by(menu, "change_pct", largest=False),
    "change_high": lambda menu: _pick_by(menu, "change_pct", largest=True),
    # The richer feature set (features.py), 2026-09-16. No-ops on a menu that
    # does not carry the feature -- so they cost nothing until the live screen
    # ships it, and switch on with no second definition when it does. Each is
    # validated over six months by feature_replay before that happens.
    "ret_7d_top": lambda menu: _pick_by(menu, "ret_7d", largest=True),
    "ret_7d_low": lambda menu: _pick_by(menu, "ret_7d", largest=False),
    "rs_7d_top": lambda menu: _pick_by(menu, "rs_7d", largest=True),
    "rs_7d_low": lambda menu: _pick_by(menu, "rs_7d", largest=False),
    "rs_24h_top": lambda menu: _pick_by(menu, "rs_24h", largest=True),
    "rs_24h_low": lambda menu: _pick_by(menu, "rs_24h", largest=False),
    "vol_high": lambda menu: _pick_by(menu, "vol_24h_pct", largest=True),
    "vol_low": lambda menu: _pick_by(menu, "vol_24h_pct", largest=False),
    "near_7d_high": lambda menu: _pick_by(menu, "from_7d_high_pct", largest=True),
    "far_from_7d_high": lambda menu: _pick_by(menu, "from_7d_high_pct", largest=False),
    "vol_surge_top": lambda menu: _pick_by(menu, "vol_ratio_24h", largest=True),
    "taker_24h_top": lambda menu: _pick_by(menu, "taker_share_24h", largest=True),
    "taker_24h_low": lambda menu: _pick_by(menu, "taker_share_24h", largest=False),
}


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


def profitable(row: dict) -> bool | None:
    """Did the position END IN PROFIT AFTER COSTS -- the thing `confidence` is?

    The prompt defines confidence as the probability the position ends in
    profit by the trailing stop, the target, or the time stop. Grading it
    against `cleared_target` (the full +17% target before the stop, an event
    the same prompt says ends ~7% of positions) told a calibrated 0.50 stater
    every cycle that it hit 7% -- "overconfident" in every band by
    construction -- and the model did what a well-behaved model does with that
    feedback: it declined ~95% of cycles. The self-censoring the calibration
    loop was built to correct, it was causing (found 2026-09-16).

    A stop is a loss whatever the horizon-end return says: the position was
    closed at the stop and never saw the horizon. A target is profit. A time
    exit is profit iff the horizon return cleared the hurdle. The trail is not
    modelled at resolve time (only its endpoints are stored), so this is the
    hold-to-horizon reading of the prompt's definition, stated as such.
    """
    outcome = row.get("outcome")
    if outcome == "stop":
        return False
    if outcome == "target":
        return True
    if outcome == "time":
        return _is_true(row.get("cleared_hurdle"))
    return None


def _parse_ts(value: str) -> dt.datetime | None:
    """A decision timestamp, or None when it is unusable.

    An unparseable stamp must not silently become "long ago" -- that would let
    the de-overlap filter wave every malformed row through as independent.
    """
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def independent(rows: list[dict], horizon: dt.timedelta) -> list[dict]:
    """Drop observations that re-measure a symbol already in flight.

    A pick held for `horizon` and re-opened an hour later is not a second
    trial -- it is the SAME price path scored twice. The live arms do this
    constantly (the model named HEMIUSDT on 117 of 615 Binance decisions), so
    without this every statistic downstream reports a sample size it does not
    have, and the arm that concentrates is penalised for concentrating.

    This is methodology trap #2 from *Research findings*, which `backfill.py`
    already avoids by construction and the `universe` pass avoids by opening one
    observation per symbol at a time. The live picks had no such guard.

    Oldest-first so the FIRST sighting is the one kept; a row with an unusable
    timestamp is dropped rather than waved through as independent.
    """
    last: dict[str, dt.datetime] = {}
    keep: list[dict] = []
    for row in sorted(rows, key=lambda r: str(r.get("ts") or "")):
        when = _parse_ts(str(row.get("ts") or ""))
        if when is None:
            continue
        symbol = str(row.get("symbol"))
        if symbol in last and when - last[symbol] < horizon:
            continue
        last[symbol] = when
        keep.append(row)
    return keep


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
            # The second-opinion LLM, scored as its own selector arm.
            deep_pick = rec.get("virtual_pick_deep")
            deep_tier = rec.get("second_opinion_tier")
            if deep_pick and deep_tier:
                count += self._open_pick(
                    f"{ARM_PREFIX}llm_{deep_tier}",
                    deep_pick,
                    ts,
                    features,
                    opens,
                    resolves,
                    venue=venue,
                    confidence=rec.get("virtual_confidence_deep"),
                )
            # The selector arms, from the SAME menu the model and shadow saw.
            menu = rec.get("candidates") or []
            for name in self.scfg.arms:
                selector = SELECTORS.get(name)
                if selector is None:
                    log.warning("unknown selector arm %r; known: %s", name, sorted(SELECTORS))
                    continue
                pick = selector(menu)
                if pick:
                    count += self._open_pick(
                        f"{ARM_PREFIX}{name}", pick, ts, features, opens, resolves, venue
                    )
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
        """Paired model-vs-shadow: same decision, same venue, both resolved.

        **Pairs are DE-OVERLAPPED before anything is computed from them**, and
        that is not a refinement -- it inverts the answer. The model names the
        same symbol over and over (HEMIUSDT was 117 of 615 Binance decisions,
        19%), so its picks resolve on 72h windows of ONE price path counted
        dozens of times, while the shadow drawing uniformly from a 25-name menu
        spreads over twice as many symbols. Counting every decision as an
        independent trial therefore does not merely inflate n -- it biases the
        comparison ASYMMETRICALLY against the concentrated arm, and hands the
        bootstrap a sample size it does not have, so the interval comes back
        tight and confident and wrong. Measured 2026-09-16 on the live corpus:

            Binance   raw n=615  model -5.28% vs shadow -2.62%  (edge -2.66%)
                      indep n=68 model -2.16% vs shadow -3.13%  (edge +0.97%)

        This is methodology trap #2 from *Research findings* -- the one that
        produced "+7.4% per trade" and vanished on non-overlapping entries --
        living inside the gate's own blocking criterion for two weeks.

        The rule: walking pairs oldest-first, a pair is kept only if NEITHER
        side's symbol has been used by a kept pair within `horizon_minutes`.
        Both sides, because a difference is only as independent as its more
        dependent half. `n_raw` is reported alongside `n` so the shrinkage is
        visible rather than silently absorbed.
        """

        def key(r):
            return (str(r.get("venue") or "BINANCE"), r.get("decision_ts"))

        m = {key(r): r for r in model}
        s = {key(r): r for r in shadow}
        horizon = dt.timedelta(minutes=self.scfg.horizon_minutes)
        candidates = sorted(m.keys() & s.keys(), key=lambda k: str(k[1] or ""))
        pairs: list[tuple[float, float]] = []
        n_raw = len(candidates)
        last: dict[tuple[str, str], dt.datetime] = {}
        for k in candidates:
            when = _parse_ts(str(k[1] or ""))
            rows = {"model": m[k], "shadow": s[k]}
            marks = {side: (side, str(row.get("symbol"))) for side, row in rows.items()}
            if when is not None and any(
                mark in last and when - last[mark] < horizon for mark in marks.values()
            ):
                continue
            if when is not None:
                for mark in marks.values():
                    last[mark] = when
            pairs.append((float(m[k]["forward_return_pct"]), float(s[k]["forward_return_pct"])))
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
            "n_raw": n_raw,
            "model_avg_pct": round(sum(p[0] for p in pairs) / n, 3) if n else None,
            "shadow_avg_pct": round(sum(p[1] for p in pairs) / n, 3) if n else None,
            "mean_diff_pct": round(sum(diffs) / n, 3) if n else None,
            "median_diff_pct": round(statistics.median(diffs), 3) if n else None,
            "model_wins": sum(1 for d in diffs if d > 0),
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
            "ci_level": self.scfg.ci_level,
        }

    def _screen_control(self, rows: list[dict]) -> dict:
        """Is the SCREEN earning its place? Nothing measured this until now.

        The shadow pick answers "does the model beat chance INSIDE the menu".
        It cannot answer "is the menu worth having", because both arms are drawn
        from it -- so a menu that selects losers makes the model and its control
        lose together and the comparison stays silent about the cause. That is
        the control group missing one level up, and on the first reading it was
        the largest effect in the system (2026-09-16, identical window, same
        resolution machinery, de-overlapped, excess vs each book's benchmark):

            menu   (shadow: a RANDOM draw from the screen's own menu)  -2.72%
            pool   (the explore arm, drawn outside the screen)         +1.37%
            universe (broad sample of tradable symbols)                +0.76%

        A random draw from the menu lost ~3.5% to a random draw from outside it,
        which is an order of magnitude more than any model-vs-shadow edge ever
        measured here. `shadow` is the honest probe of the menu precisely
        because no model touches it.

        Reported as a DIAGNOSTIC, never a gate criterion: what to do about a bad
        screen is a strategy decision, and this only measures that there is one.
        """
        groups = {"menu": ("shadow",), "pool": ("random",), "universe": ("universe",)}
        out: dict[str, dict] = {}
        for venue in sorted({str(r.get("venue") or "BINANCE") for r in rows}):
            here = [r for r in rows if str(r.get("venue") or "BINANCE") == venue]
            cell = {}
            for label, sources in groups.items():
                members = [
                    r
                    for r in here
                    if r.get("source") in sources and r.get("excess_return_pct") is not None
                ]
                if not members:
                    continue
                excess = [float(r["excess_return_pct"]) for r in members]
                cell[label] = {
                    "n": len(members),
                    "avg_excess_pct": round(statistics.fmean(excess), 3),
                    "median_excess_pct": round(statistics.median(excess), 3),
                    "clear_rate": round(
                        sum(1 for r in members if _is_true(r.get("cleared_hurdle"))) / len(members),
                        3,
                    ),
                }
            if "menu" in cell and "pool" in cell:
                cell["menu_minus_pool_pct"] = round(
                    cell["menu"]["avg_excess_pct"] - cell["pool"]["avg_excess_pct"], 3
                )
            if cell:
                out[venue] = cell
        return out

    def _calibration(self, model_rows: list[dict]) -> list[dict]:
        """Stated confidence vs realised PROFIT AFTER COSTS, per band.

        `hit` means what the prompt says `confidence` means (see `profitable`).
        The target-before-stop rate survives as `target_rate` so the harder
        event is still visible; it is simply no longer what the model is graded
        on, because it is not what the model was asked.
        """
        graded = [
            r for r in model_rows if r.get("confidence") is not None and profitable(r) is not None
        ]
        by_band: dict[tuple[str, str], list[dict]] = {}
        for r in graded:
            band = confidence_band(float(r["confidence"]), self.scfg.confidence_bands)
            by_band.setdefault((band, ""), []).append(r)
            by_band.setdefault((band, str(r.get("venue") or "BINANCE")), []).append(r)
        out = []
        for (band, venue), members in sorted(by_band.items()):
            hits = sum(1 for r in members if profitable(r))
            targets = sum(1 for r in members if _is_true(r.get("cleared_target")))
            n = len(members)
            out.append(
                {
                    "band": band,
                    "venue": venue,
                    "n": n,
                    "hits": hits,
                    "hit_rate": round(hits / n, 3),
                    "target_rate": round(targets / n, 3),
                    "stated": round(sum(float(r["confidence"]) for r in members) / n, 3),
                    "avg_return_pct": round(
                        sum(float(r.get("forward_return_pct", 0)) for r in members) / n, 3
                    ),
                }
            )
        return out

    def _relabel(self, rows: list[dict]) -> int:
        """Relabel `cleared_hurdle` AGAINST THE CURRENT HURDLE. It is written
        into the resolve row at resolve time, so every row resolved before the
        2026-09-15 slippage re-measure was graded against the old 0.500% /
        0.600% bar -- and this label is the fit target, the bucket clear rate
        and part of what the prompt reads back. `pnl` and `promotion` already
        price fees live; this is the same principle applied one layer down:
        the journal is the append-only RECORD, and anything derived from it is
        derived now, from the config in force now. `forward_return_pct` and
        the book are both on the row, so the relabel is exact, not estimated.

        NOT relabelled, deliberately: `cleared_target` and `outcome`. Those
        grade the price PATH against the exit contract's levels, and the path
        is not stored -- only its endpoints. Redoing them needs a re-resolve
        against the price source, which is a different (and much more
        expensive) operation. The staleness is bounded and small: the hurdle
        change moved a 100-entry target from 117.50 to 116.90, because
        `min_reward_risk` against the 8% stop dominates the target, not the
        hurdle. `hurdle_pct_at_resolve` keeps the original for audit.
        """
        relabelled = 0
        for r in rows:
            if r.get("forward_return_pct") is None:
                continue
            hurdle_pct = self.ledger.breakeven_move_pct(str(r.get("book") or "BINANCE")) * 100
            cleared = float(r["forward_return_pct"]) > hurdle_pct
            if cleared != _is_true(r.get("cleared_hurdle")):
                relabelled += 1
            r["hurdle_pct_at_resolve"] = r.get("hurdle_pct")
            r["hurdle_pct"] = round(hurdle_pct, 4)
            r["cleared_hurdle"] = cleared
        return relabelled

    def _aggregate(self, opens: dict, resolves: dict) -> dict:
        rows = []
        for obs_id, res in resolves.items():
            open_rec = opens.get(obs_id)
            if open_rec:
                rows.append({**open_rec, **res})
        for r in rows:
            r.setdefault("venue", _venue_of_book(r.get("book") or ""))

        relabelled = self._relabel(rows)

        # De-overlap the LIVE arms before a single statistic is computed from
        # them. The backtest sources are built non-overlapping by `backfill.py`
        # and the `universe` pass opens one observation per symbol at a time;
        # only the model/shadow/random picks re-measure a symbol mid-flight, and
        # they do it constantly. Buckets, clear rates and calibration all read
        # these rows, so leaving it to the paired comparison alone would fix the
        # verdict and leave the PROMPT quoting inflated evidence back to itself.
        horizon = dt.timedelta(minutes=self.scfg.horizon_minutes)
        deduped: list[dict] = []
        overlap_dropped: dict[str, int] = {}
        by_source_raw: dict[str, list[dict]] = {}
        for r in rows:
            by_source_raw.setdefault(r.get("source", "?"), []).append(r)
        for source, members in by_source_raw.items():
            if source not in LIVE_SOURCES and not source.startswith(ARM_PREFIX):
                deduped.extend(members)
                continue
            kept = independent(members, horizon)
            overlap_dropped[source] = len(members) - len(kept)
            deduped.extend(kept)
        rows = deduped

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

        # De-overlapped, for the calibration block the prompt reads back.
        model_rows = by_source.get("model", [])
        # The model's record ON THE CURRENT MENU, as its own labelled rows. The
        # full-epoch rows above still render; this adds, never replaces.
        since = self.scfg.model_record_since
        recent = [r for r in model_rows if since and str(r.get("ts") or "") >= since]
        calibration_since: list[dict] = []
        if recent:
            buckets.append(self._bucket(f"model picks since {since}", recent))
            for venue_ in sorted({r.get("venue") for r in recent}):
                buckets.append(
                    self._bucket(
                        f"model picks since {since}:{venue_}",
                        [r for r in recent if r.get("venue") == venue_],
                    )
                )
            calibration_since = [{**c, "since": since} for c in self._calibration(recent)]
        # The PAIRED test starts from the RAW rows, not the de-overlapped ones.
        # De-overlapping each arm on its own drops one side of a decision and
        # the surviving side then pairs with nothing -- it would silently shrink
        # the comparison while looking like it had merely deduplicated. `_pairs`
        # applies its own rule, which keeps a pair only when BOTH sides are
        # independent, so the pairing is preserved by construction.
        model_raw = by_source_raw.get("model", [])
        shadow_raw = by_source_raw.get("shadow", [])
        pair_summary = self._pairs(model_raw, shadow_raw)
        pairs_by_venue = {}
        for venue in sorted(
            {r.get("venue") for r in model_raw} | {r.get("venue") for r in shadow_raw}
        ):
            pv = self._pairs(
                [r for r in model_raw if r.get("venue") == venue],
                [r for r in shadow_raw if r.get("venue") == venue],
            )
            if pv["n"]:
                pairs_by_venue[venue] = pv

        # THE LEADERBOARD. Every selector -- the LLM included, as `model` --
        # paired against the same shadow with the same de-overlap rule. Sorted
        # by the CI's LOWER bound, because a point estimate at n~100 is luck
        # not yet ruled out and this repo has already promoted one of those.
        leaderboard = []
        for source, source_rows in sorted(by_source_raw.items()):
            if source != "model" and not source.startswith(ARM_PREFIX):
                continue
            pv = self._pairs(source_rows, shadow_raw)
            if pv["n"]:
                leaderboard.append({"selector": source, **pv})
        leaderboard.sort(
            key=lambda r: r["ci_low"] if r.get("ci_low") is not None else -1e9, reverse=True
        )

        return {
            "meta": {
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                "horizon_minutes": self.scfg.horizon_minutes,
                "horizons": {
                    v: int(self.horizon_for(v).total_seconds() // 60) for v in self.scfg.venues
                },
                "min_bucket_n": self.scfg.min_bucket_n,
                "resolved_observations": len(rows),
                # How many live observations were re-measurements of a symbol
                # already in flight. Large numbers here are not a defect in the
                # arms -- they are why the statistics must de-overlap.
                "overlapping_dropped": overlap_dropped,
                # Rows whose hurdle verdict FLIPPED against the hurdle in force
                # now. Non-zero means the stored labels were stale, which is
                # expected after any `market_fees` edit.
                "relabelled_against_current_hurdle": relabelled,
                "benchmarks": dict(self.scfg.benchmarks),
            },
            "buckets": buckets,
            "model_vs_shadow": pair_summary,
            "model_vs_shadow_by_venue": pairs_by_venue,
            "calibration": self._calibration(model_rows),
            "calibration_since": calibration_since,
            "screen_control": self._screen_control(rows),
            "leaderboard": leaderboard,
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
        if label.startswith(ARM_PREFIX):
            continue  # the leaderboard is the operator's instrument, not context
        # "<source> picks:<venue>" is a per-venue split of a live source;
        # "backtest_kr:KR" is a book split and renders as it is.
        if label.startswith(tuple(f"{src} picks:" for src in LIVE_SOURCES)) or (
            label.startswith("model picks since") and ":" in label
        ):
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
    for c in list(data.get("calibration", [])) + list(data.get("calibration_since", [])):
        if c.get("n", 0) < min_n:
            continue
        if c.get("venue") and c["venue"] != venue:
            continue
        label = f"confidence {c['band']}"
        if c.get("since"):
            label += f" since {c['since']}"
        if c.get("venue"):
            label += " (this venue)"
        calibration[label] = (
            f"{c['hits']}/{c['n']} ended in profit after costs ({c['hit_rate']:.0%}) "
            f"against a stated {c['stated']:.2f}; "
            f"{c.get('target_rate', 0):.0%} reached the full target; "
            f"avg {c['avg_return_pct']:+.2f}%"
        )
    # The six-month feature replay (agent/feature_replay.py): decile spreads
    # whose CI excludes zero, and the pool's return by regime. Backtest
    # provenance is in the label. Silence for anything that did not measure.
    priors: dict[str, str] = {}
    try:
        fr = json.loads(Path(cfg.score.feature_replay_output).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fr = {}
    for d in fr.get("deciles", []):
        if d.get("ci_low") is not None and (d["ci_low"] > 0 or d["ci_high"] < 0):
            priors[f"backtest decile {d['feature']}"] = (
                f"top 10% {d['top_decile_excess_pct']:+.2f}% vs bottom 10% "
                f"{d['bottom_decile_excess_pct']:+.2f}% excess at 72h (n={d['n']}, "
                f"95% CI {d['ci_low']:+.2f}..{d['ci_high']:+.2f})"
            )
    for r in fr.get("regime", []):
        if r.get("state") in ("btc_7d_up", "btc_7d_down"):
            priors[f"backtest regime {r['state']}"] = (
                f"pool 72h return {r['pool_raw_pct']:+.2f}% (median "
                f"{r['median_raw_pct']:+.2f}%) over {r['n_sections']} weeks"
            )
    if not rows and not calibration and not priors:
        return None
    horizon = data.get("meta", {}).get("horizon_minutes")
    block = {
        "note": (
            f"Measured on this system's own forward returns at each venue's hold horizon "
            f"({horizon}-minute on Binance), from the venue's own price record. Pooled "
            "rows span every venue; '(this venue)' rows are this market alone. "
            "'vs benchmark' is the excess over the book's benchmark across the same "
            "window. Small samples are withheld."
            + (
                f" The menu's construction changed on {since}: rows marked 'since' are "
                "your record on the CURRENT menu; unmarked rows include the retired one."
                if (since := cfg.score.model_record_since)
                else ""
            )
        ),
        "record": rows,
        **({"backtest_priors": priors} if priors else {}),
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
