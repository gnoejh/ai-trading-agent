"""Outcome scoring — the build step that fills the experience store (the RAG).

Nothing here decides anything. The scorer turns two append-only records the
system already produces — the decision journal and its own observation log —
into measured aggregates the decide prompt can cite:

    open   : first sighting of a symbol (universe sweep, model pick, shadow
             random pick, or a random-arm entry) with its features and the
             mainnet price at that moment
    resolve: once the horizon elapses, the forward return from mainnet klines,
             plus the max run-up and drawdown along the way

De-overlapped per symbol: one open universe observation at a time, because
scoring every 15-minute sighting of the same rally as independent evidence is
methodology trap #2 and manufactures fake edges. Aggregates carry their sample
size, and `experience_block` refuses to render any bucket below
`score.min_bucket_n` — an unfilled store must say nothing, not guess.

Runs inside the service between cycles (`maybe_run`), or standalone:

    uv run python -m trading.agent.scorer
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

from trading.config import AppConfig, config

log = logging.getLogger(__name__)

# Signed 24h-change bands for bucketing, in percent. The screen's entry band is
# 15..60; bands on both sides of it exist so the screen itself can be judged.
CHANGE_BANDS = ((None, 0.0), (0.0, 15.0), (15.0, 40.0), (40.0, None))


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


class ExperienceScorer:
    def __init__(self, client, screen, ledger, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.scfg = self.cfg.score
        self.client = client  # data-plane reads go to mainnet by construction
        self.screen = screen
        self.ledger = ledger
        self.obs_path = Path(self.scfg.observations)
        self.exp_path = Path(self.scfg.experience)
        self.journal_path = Path(self.cfg.agent.journal)
        self._last_run = 0.0

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
        from_universe = self._open_universe(opens, resolves)
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
        """Model picks, shadow random picks and random-arm entries.

        The journal is re-read from the start every pass and observation ids are
        derived from the journalled timestamps, so this is idempotent — no
        cursor to lose, no double counting.
        """
        if not self.journal_path.exists():
            return 0
        count = 0
        with self.journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts", "")
                if rec.get("kind") == "decision":
                    features = {c.get("symbol"): c for c in rec.get("candidates", [])}
                    picked = {
                        v["intent"]["symbol"]
                        for v in rec.get("verdicts", [])
                        if str(v.get("intent", {}).get("side", "")).upper().endswith("BUY")
                    }
                    # The virtual pick is the model's top candidate on EVERY
                    # decision, declines included — it grows the model-vs-random
                    # corpus at decision rate instead of trade rate. Traded picks
                    # still open below; identical ids dedupe.
                    virtual = rec.get("virtual_pick")
                    if virtual:
                        count += self._open_pick("model", virtual, ts, features, opens, resolves)
                    for symbol in picked:
                        count += self._open_pick("model", symbol, ts, features, opens, resolves)
                    shadow = rec.get("shadow_random")
                    if shadow:
                        count += self._open_pick("shadow", shadow, ts, features, opens, resolves)
                elif rec.get("kind") == "explore" and rec.get("sent"):
                    entry = rec.get("entry") or {}
                    symbol = entry.get("symbol")
                    if symbol:
                        count += self._open_pick(
                            "random", symbol, ts, {symbol: entry}, opens, resolves
                        )
        return count

    def _open_pick(
        self, source: str, symbol: str, ts: str, features: dict, opens: dict, resolves: dict
    ) -> int:
        obs_id = f"{source}:{symbol}:{ts}"
        if obs_id in opens or obs_id in resolves:
            return 0
        f = features.get(symbol) or {}
        price = float(f.get("price") or 0)
        if price <= 0:
            return 0  # no anchor price, no observation
        rec = {
            "kind": "open",
            "id": obs_id,
            "source": source,
            "symbol": symbol,
            "ts": ts,
            "decision_ts": ts,
            "price": price,
            "book": f.get("book", ""),
            "change_pct": float(f.get("change_pct") or 0),
            "quote_volume": float(f.get("quote_volume") or 0),
            "taker_share": f.get("taker_buy_share"),
        }
        self._append(rec)
        opens[obs_id] = rec
        return 1

    def _open_universe(self, opens: dict, resolves: dict) -> int:
        """One open observation per tradable symbol at a time.

        order_size 0 applies only the absolute liquidity floors, so the sweep
        covers the widest pool any order size could reach.
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

    def _resolve_due(self, opens: dict, resolves: dict) -> int:
        now = dt.datetime.now(dt.UTC)
        horizon = dt.timedelta(minutes=self.scfg.horizon_minutes)
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
            if now - opened < horizon:
                continue
            resolved = self._resolve_one(rec, opened, horizon)
            if resolved is not None:
                self._append(resolved)
                resolves[obs_id] = resolved
                count += 1
        return count

    def _resolve_one(self, rec: dict, opened: dt.datetime, horizon: dt.timedelta) -> dict | None:
        entry = float(rec.get("price") or 0)
        if entry <= 0:
            return None
        hours = max(int(horizon.total_seconds() // 3600), 1)
        try:
            bars = self.client.call(
                "klines",
                {
                    "symbol": rec["symbol"],
                    "interval": "1h",
                    "startTime": int(opened.timestamp() * 1000),
                    "limit": min(hours + 2, 1000),
                },
            ).body.get("rows", [])
        except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
            log.warning("resolve %s failed: %s", rec.get("id"), exc)
            return None
        window = [b for b in bars if len(b) > 4][:hours]
        if not window:
            return None
        end_price = float(window[-1][4])  # close of the last bar inside the horizon
        highs = [float(b[2]) for b in window]
        lows = [float(b[3]) for b in window]
        hurdle = self.ledger.breakeven_move_pct(rec.get("book") or "BINANCE")
        forward = end_price / entry - 1
        return {
            "kind": "resolve",
            "id": rec["id"],
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "end_price": end_price,
            "forward_return_pct": round(forward * 100, 4),
            "max_runup_pct": round((max(highs) / entry - 1) * 100, 4),
            "max_drawdown_pct": round((min(lows) / entry - 1) * 100, 4),
            "hurdle_pct": round(hurdle * 100, 4),
            "cleared_hurdle": forward > hurdle,
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

    def _aggregate(self, opens: dict, resolves: dict) -> dict:
        rows = []
        for obs_id, res in resolves.items():
            open_rec = opens.get(obs_id)
            if open_rec:
                rows.append({**open_rec, **res})

        def bucket(label: str, members: list[dict]) -> dict:
            n = len(members)
            cleared = sum(1 for m in members if m.get("cleared_hurdle"))
            avg = sum(m.get("forward_return_pct", 0) for m in members) / n if n else 0.0
            return {"label": label, "n": n, "cleared": cleared, "avg_return_pct": round(avg, 3)}

        buckets = []
        by_source: dict[str, list[dict]] = {}
        for r in rows:
            by_source.setdefault(r.get("source", "?"), []).append(r)
        for source, members in sorted(by_source.items()):
            buckets.append(bucket(f"{source} picks", members))

        universe = by_source.get("universe", [])
        by_book: dict[str, list[dict]] = {}
        for r in universe:
            by_book.setdefault(r.get("book") or "?", []).append(r)
        for book, members in sorted(by_book.items()):
            buckets.append(bucket(f"universe:{book}", members))

        for lo, hi in CHANGE_BANDS:
            members = [r for r in universe if _in_band(float(r.get("change_pct") or 0), lo, hi)]
            buckets.append(bucket(f"universe 24h-change {_band_label(lo, hi)}", members))

        flowed = sorted(
            (r for r in universe if r.get("taker_share") is not None),
            key=lambda r: r["taker_share"],
        )
        if flowed:
            k = max(len(flowed) // 3, 1)
            for label, members in (
                ("flow tertile low", flowed[:k]),
                ("flow tertile high", flowed[-k:]),
            ):
                buckets.append(bucket(f"universe {label}", members))

        closed = self._trade_outcomes()
        if closed:
            buckets.append(bucket("closed trades (mark-to-mainnet)", closed))

        # Paired model-vs-shadow: same decision timestamp, both resolved.
        model = {r.get("decision_ts"): r for r in by_source.get("model", [])}
        shadow = {r.get("decision_ts"): r for r in by_source.get("shadow", [])}
        pairs = [
            (model[ts]["forward_return_pct"], shadow[ts]["forward_return_pct"])
            for ts in model.keys() & shadow.keys()
        ]
        pair_summary = {
            "n": len(pairs),
            "model_avg_pct": round(sum(p[0] for p in pairs) / len(pairs), 3) if pairs else None,
            "shadow_avg_pct": round(sum(p[1] for p in pairs) / len(pairs), 3) if pairs else None,
        }

        return {
            "meta": {
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                "horizon_minutes": self.scfg.horizon_minutes,
                "min_bucket_n": self.scfg.min_bucket_n,
                "resolved_observations": len(rows),
            },
            "buckets": buckets,
            "model_vs_shadow": pair_summary,
        }


def experience_block(cfg: AppConfig | None = None) -> dict | None:
    """The prompt-side read: qualifying aggregates only, or None.

    An unfilled store must say nothing — rendering a half-empty record would
    hand the model confident-sounding noise. Every rendered row carries its n.
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
    rows = {
        b["label"]: f"{b['cleared']}/{b['n']} cleared hurdle, avg {b['avg_return_pct']:+.2f}%"
        for b in data.get("buckets", [])
        if b.get("n", 0) >= min_n
    }
    pairs = data.get("model_vs_shadow", {})
    if pairs.get("n", 0) >= min_n:
        rows["model vs random (paired)"] = (
            f"n={pairs['n']}: model {pairs['model_avg_pct']:+.2f}% "
            f"vs random {pairs['shadow_avg_pct']:+.2f}%"
        )
    if not rows:
        return None
    horizon = data.get("meta", {}).get("horizon_minutes")
    return {
        "note": (
            f"Measured on this system's own {horizon}-minute forward returns "
            "(mainnet prices). Small samples are withheld."
        ),
        "record": rows,
    }


def main() -> int:
    import argparse

    from trading.brokers.adapters import build_adapter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="binance", choices=["binance"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    adapter = build_adapter(args.broker, None, cfg)
    from trading.accounting.costs import CostLedger

    scorer = ExperienceScorer(adapter.client, adapter.screen, CostLedger(cfg), cfg)
    stats = scorer.run_once()
    print(json.dumps(stats, indent=1))
    block = experience_block(cfg)
    print(
        json.dumps(block, ensure_ascii=False, indent=1) if block else "(no qualifying buckets yet)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
