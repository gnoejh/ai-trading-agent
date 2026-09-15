"""Experiences as CASES: the k nearest past setups to this one, and what happened.

    uv run python -m trading.agent.similar            # validate over the backtest corpus
    uv run python -m trading.agent.similar --build    # write the live index

The store held 54,000 resolved observations with features and the prompt
rendered them as a census -- "24h-change 0..15%: 47% cleared". An expert uses
experience differently: "the twenty most similar setups to this one cleared
costs 61% of the time". This module is that: a standardised feature vector per
resolved observation, a nearest-neighbour query, and an outcome distribution.

Two disciplines, both structural:

* TEMPORAL: a query only ever sees neighbours that RESOLVED before it was
  opened. In validation that is enforced per query (neighbours strictly
  earlier); live, every neighbour is a resolved row and the query is now.
  Without this the validation is lookahead dressed as retrieval.
* MEASURED FIRST: `validate()` scores a sample of backtest rows by their
  neighbours' hit rate and asks whether that score has a decile spread in
  realised excess return, with a CI. The prompt gets `similar_setups` only if
  it does (`score.similar_k > 0`), and each rendered line carries its n.

Pure Python by design (the repo has no numpy); the validation subsamples
queries so it finishes in minutes, the live query is 25 candidates against
the index and takes seconds.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from pathlib import Path

from trading.agent.feature_replay import load_features
from trading.agent.scorer import _is_true
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def vector(row: dict, keys: list[str]) -> list[float] | None:
    """The raw feature vector, or None if any key is missing -- a case with a
    hole in it is not comparable, and filling it would invent similarity."""
    out = []
    for k in keys:
        if k == "log_turnover":
            qv = _f(row.get("quote_volume"))
            if not qv or qv <= 0:
                return None
            out.append(math.log(qv))
            continue
        v = _f(row.get(k))
        if v is None:
            v = _f(row.get("taker_buy_share")) if k == "taker_share" else None
        if v is None:
            return None
        out.append(v)
    return out


class SimilarIndex:
    def __init__(self, keys: list[str], rows: list[dict]):
        self.keys = keys
        self.ts: list[str] = []
        self.vecs: list[list[float]] = []
        self.hit: list[bool] = []
        self.ret: list[float] = []
        self.excess: list[float | None] = []
        for r in rows:
            v = vector(r, keys)
            if v is None or r.get("forward_return_pct") is None:
                continue
            self.ts.append(str(r.get("ts") or ""))
            self.vecs.append(v)
            self.hit.append(_is_true(r.get("cleared_hurdle")))
            self.ret.append(float(r["forward_return_pct"]))
            self.excess.append(_f(r.get("excess_return_pct")))
        n = len(self.vecs)
        dims = len(keys)
        self.mean = [sum(v[d] for v in self.vecs) / n if n else 0.0 for d in range(dims)]
        self.std = [
            math.sqrt(sum((v[d] - self.mean[d]) ** 2 for v in self.vecs) / max(n - 1, 1)) or 1.0
            for d in range(dims)
        ]
        self.z = [[(v[d] - self.mean[d]) / self.std[d] for d in range(dims)] for v in self.vecs]

    def __len__(self) -> int:
        return len(self.vecs)

    def standardise(self, v: list[float]) -> list[float]:
        return [(v[d] - self.mean[d]) / self.std[d] for d in range(len(self.keys))]

    def query(self, raw: list[float], k: int, *, before: str | None = None) -> dict | None:
        """Outcome distribution of the k nearest cases (resolved before `before`)."""
        q = self.standardise(raw)
        scored = []
        for i, z in enumerate(self.z):
            if before is not None and self.ts[i] >= before:
                continue
            d = 0.0
            for a, b in zip(q, z, strict=True):
                d += (a - b) * (a - b)
            scored.append((d, i))
        if len(scored) < k:
            return None
        scored.sort()
        idx = [i for _, i in scored[:k]]
        ex = [self.excess[i] for i in idx if self.excess[i] is not None]
        return {
            "n": k,
            "hit_rate": round(sum(1 for i in idx if self.hit[i]) / k, 3),
            "avg_return_pct": round(statistics.fmean(self.ret[i] for i in idx), 3),
            "avg_excess_pct": round(statistics.fmean(ex), 3) if ex else None,
            "median_return_pct": round(statistics.median(self.ret[i] for i in idx), 3),
        }

    def to_json(self) -> dict:
        return {
            "keys": self.keys,
            "mean": self.mean,
            "std": self.std,
            "ts": self.ts,
            "vecs": self.vecs,
            "hit": self.hit,
            "ret": self.ret,
            "excess": self.excess,
        }

    @classmethod
    def from_json(cls, data: dict) -> SimilarIndex:
        idx = cls.__new__(cls)
        idx.keys = data["keys"]
        idx.mean, idx.std = data["mean"], data["std"]
        idx.ts, idx.vecs = data["ts"], data["vecs"]
        idx.hit, idx.ret, idx.excess = data["hit"], data["ret"], data["excess"]
        dims = len(idx.keys)
        idx.z = [[(v[d] - idx.mean[d]) / idx.std[d] for d in range(dims)] for v in idx.vecs]
        return idx


def corpus(cfg: AppConfig, *, sources: tuple[str, ...]) -> list[dict]:
    """Resolved observations joined with the feature side-file where it applies."""
    opens: dict[str, dict] = {}
    resolves: dict[str, dict] = {}
    with Path(cfg.score.observations).open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "open" and r.get("source") in sources:
                opens[r["id"]] = r
            elif r.get("kind") == "resolve":
                resolves[r["id"]] = r
    feats = load_features(Path(cfg.score.backtest_features))
    funding = load_features(Path(cfg.score.backtest_funding))
    rows = []
    for oid, o in opens.items():
        res = resolves.get(oid)
        if not res:
            continue
        extra = {k: v for k, v in feats.get(oid, {}).items() if k not in ("id", "symbol", "ts")}
        extra.update(
            {k: v for k, v in funding.get(oid, {}).items() if k not in ("id", "symbol", "ts")}
        )
        rows.append({**o, **extra, **res, "ts": o["ts"]})
    return rows


def validate(cfg: AppConfig | None = None, *, sample: int = 1500) -> dict:
    """Does the neighbours' hit rate predict the outcome? Leave-the-future-out."""
    cfg = cfg or config()
    keys = list(cfg.score.similar_features)
    rows = [r for r in corpus(cfg, sources=("backtest",)) if r.get("book") == "CRYPTO"]
    rows.sort(key=lambda r: r["ts"])
    index = SimilarIndex(keys, rows)
    k = cfg.score.similar_k or 30
    usable = [(r, vector(r, keys)) for r in rows]
    usable = [(r, v) for r, v in usable if v is not None]
    step = max(1, len(usable) // sample)
    scored = []
    # Excess vs the section's BTC is not on backtest rows; use the raw forward
    # return minus the same-timestamp BTC return where available.
    btc = {r["ts"]: r["forward_return_pct"] for r in rows if r["symbol"] == "BTCUSDT"}
    for r, v in usable[::step]:
        q = index.query(v, k, before=r["ts"])
        if q is None:
            continue
        bench = btc.get(r["ts"])
        excess = r["forward_return_pct"] - bench if bench is not None else None
        scored.append((q["hit_rate"], _is_true(r.get("cleared_hurdle")), excess))
    if len(scored) < 200:
        return {"n": len(scored), "error": "too few scored rows"}
    scored.sort(key=lambda t: t[0])
    tenth = len(scored) // 10
    top, bottom = scored[-tenth:], scored[:tenth]
    top_ex = [e for _, _, e in top if e is not None]
    bot_ex = [e for _, _, e in bottom if e is not None]
    diffs_boot = None
    if top_ex and bot_ex:
        import random

        rng = random.Random(cfg.score.bootstrap_seed)
        draws = sorted(
            statistics.fmean(rng.choice(top_ex) for _ in top_ex)
            - statistics.fmean(rng.choice(bot_ex) for _ in bot_ex)
            for _ in range(1000)
        )
        diffs_boot = (round(draws[25], 3), round(draws[974], 3))
    # Calibration of the score itself: predicted hit rate vs realised, by band.
    bands = {}
    for score, hit, _ in scored:
        b = f"{int(score * 5) / 5:.1f}-{int(score * 5) / 5 + 0.2:.1f}"
        bands.setdefault(b, []).append(hit)
    return {
        "n": len(scored),
        "k": k,
        "keys": keys,
        "index_size": len(index),
        "top_decile_excess_pct": round(statistics.fmean(top_ex), 3) if top_ex else None,
        "bottom_decile_excess_pct": round(statistics.fmean(bot_ex), 3) if bot_ex else None,
        "spread_pct": round(statistics.fmean(top_ex) - statistics.fmean(bot_ex), 3)
        if top_ex and bot_ex
        else None,
        "ci_low": diffs_boot[0] if diffs_boot else None,
        "ci_high": diffs_boot[1] if diffs_boot else None,
        "top_decile_hit_rate": round(sum(1 for _, h, _ in top if h) / len(top), 3),
        "bottom_decile_hit_rate": round(sum(1 for _, h, _ in bottom if h) / len(bottom), 3),
        "calibration": {
            b: {"n": len(v), "realised": round(sum(v) / len(v), 3)}
            for b, v in sorted(bands.items())
        },
    }


def build(cfg: AppConfig | None = None) -> dict:
    """Write the live index: every resolved case with the full feature set."""
    cfg = cfg or config()
    keys = list(cfg.score.similar_features)
    rows = corpus(cfg, sources=tuple(cfg.score.similar_sources))
    index = SimilarIndex(keys, rows)
    out = Path(cfg.score.similar_index)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index.to_json()), encoding="utf-8")
    return {"cases": len(index), "keys": keys, "path": str(out)}


def load_index(cfg: AppConfig | None = None) -> SimilarIndex | None:
    cfg = cfg or config()
    p = Path(cfg.score.similar_index)
    if not p.exists():
        return None
    try:
        return SimilarIndex.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def annotate(candidates: list[dict], index: SimilarIndex, k: int) -> int:
    """Attach `similar_setups` to each candidate that has the full vector."""
    n = 0
    for c in candidates:
        v = vector(c, index.keys)
        if v is None:
            continue
        q = index.query(v, k)
        if q is not None:
            c["similar_setups"] = q
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Case-based retrieval over resolved observations.")
    ap.add_argument(
        "--build", action="store_true", help="write the live index instead of validating"
    )
    ap.add_argument("--sample", type=int, default=1500)
    args = ap.parse_args(argv)
    cfg = config()
    if args.build:
        print(build(cfg))
        return 0
    result = validate(cfg, sample=args.sample)
    print(json.dumps(result, indent=1))
    out = Path(cfg.score.similar_validation)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
