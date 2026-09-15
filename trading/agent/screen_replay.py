"""Replay menu construction over the backtest corpus: did the screen always cost?

    uv run python -m trading.agent.screen_replay

The live screen control (2026-09-16) found that a RANDOM draw from the screen's
menu lost ~4% of excess return to a random draw from outside it, on Binance and
KR alike -- measured over one fortnight. This asks whether that is structural
or a regime accident, without waiting 72h for the first live reading on the new
menu: the `backtest` observations already on disk carry every feature the
screen ranks on (24h change, 24h turnover, 5-day taker share), computed from
bars up to t only, plus the 72h forward return -- and BTCUSDT is among them, so
the benchmark is in the same rows. Menu rules are applied to each historical
cross-section exactly as `BinanceScreen.candidates()` applies them live, and
the expected return of a random draw from a menu is the mean over its members.

Three rules, replayed side by side on identical cross-sections:

    old_0909   |change| <= 15%, volume-head ∪ flow-head, top 18   (09-09 .. 09-16)
    old_0830   15% <= |change| <= 60%, same union                 (08-30 .. 09-09)
    sample     stride across the liquidity-ordered pool, 25       (since 09-16)
    pool       every liquidity-qualified name                     (the control)

By the repo's own rule this is a PRIOR, never a criterion: it inherits the
backfill's survivorship bias (today's pool, so delisted names are missing) and
the pool it draws from is today's tradable set, not each day's. What it can say
is whether the screen's cost persists across six months of regimes; the live
control remains the confirming measurement.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import defaultdict
from pathlib import Path

from trading.agent.scorer import SELECTORS, bootstrap_ci
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

BENCHMARK = {"CRYPTO": "BTCUSDT", "BSTOCKS": "SPYBUSDT"}


def _union_menu(rows: list[dict], slots: int, lo: float, hi: float) -> list[dict]:
    """`BinanceScreen.candidates()` as it stood before 2026-09-16, one book."""
    kept = []
    for e in rows:
        chg = abs(float(e.get("change_pct") or 0)) / 100
        if lo and chg < lo:
            continue
        if hi and chg > hi:
            continue
        if e.get("taker_share") is None:  # use_flow: unranked names drop out
            continue
        kept.append(e)
    by_volume = sorted(kept, key=lambda e: -float(e["quote_volume"]))
    by_move = sorted(kept, key=lambda e: -float(e["taker_share"]))
    scored: dict[str, dict] = {}
    for ranked, label in ((by_volume, "volume"), (by_move, "move")):
        for position, entry in enumerate(ranked[: slots * 4], start=1):
            got = scored.setdefault(
                entry["symbol"], {**entry, "best_rank": position, "screens": []}
            )
            got["best_rank"] = min(got["best_rank"], position)
            got["screens"].append(label)
    ranked = sorted(scored.values(), key=lambda e: (-len(e["screens"]), e["best_rank"]))
    return ranked[:slots]


def _sample_menu(rows: list[dict], slots: int) -> list[dict]:
    """The `sample` ranker: a stride across the liquidity-ordered pool."""
    spread = sorted(rows, key=lambda e: -float(e["quote_volume"]))
    if not spread or slots <= 0:
        return []
    want = min(len(spread), slots)
    step = len(spread) / want
    return [spread[int(i * step)] for i in range(want)]


def load_cross_sections(path: Path, source: str = "backtest") -> dict[str, list[dict]]:
    """Resolved backtest rows grouped by open timestamp: one cross-section each."""
    opens: dict[str, dict] = {}
    resolves: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "open" and r.get("source") == source:
                opens[r["id"]] = r
            elif r.get("kind") == "resolve" and r.get("id", "").startswith(f"{source}:"):
                resolves[r["id"]] = r
    groups: dict[str, list[dict]] = defaultdict(list)
    for obs_id, o in opens.items():
        res = resolves.get(obs_id)
        if not res or res.get("forward_return_pct") is None:
            continue
        if not o.get("quote_volume") or not o.get("price"):
            continue
        groups[o["ts"]].append({**o, "forward_return_pct": float(res["forward_return_pct"])})
    return groups


def replay(cfg: AppConfig | None = None) -> dict:
    cfg = cfg or config()
    scfg = cfg.agent.screen
    groups = load_cross_sections(Path(cfg.score.observations))
    min_group = cfg.score.screen_replay_min_group
    rules = {
        "old_0909": lambda rows, book: _union_menu(
            rows, {"CRYPTO": 18, "BSTOCKS": 7}[book], 0.0, 0.15
        ),
        "old_0830": lambda rows, book: _union_menu(
            rows, {"CRYPTO": 18, "BSTOCKS": 7}[book], 0.15, 0.60
        ),
        "sample": lambda rows, book: _sample_menu(rows, scfg.book_slots.get(book, scfg.candidates)),
        "pool": lambda rows, book: rows,
    }
    # per book -> rule -> list of per-cross-section excess returns of a random draw
    excess: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sizes: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    arm_diffs: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    used = 0
    for _ts, rows in sorted(groups.items()):
        by_book: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_book[str(r.get("book") or "CRYPTO")].append(r)
        for book, members in by_book.items():
            bench = next(
                (r["forward_return_pct"] for r in members if r["symbol"] == BENCHMARK.get(book)),
                None,
            )
            if bench is None or len(members) < min_group:
                continue
            used += 1
            for name, rule in rules.items():
                menu = rule(members, book)
                if not menu:
                    continue
                mean_ret = statistics.fmean(r["forward_return_pct"] for r in menu)
                excess[book][name].append(mean_ret - bench)
                sizes[book][name].append(len(menu))
            # The selector arms, exactly as the live leaderboard runs them: each
            # picks ONE name from the `sample` menu, paired against a random
            # draw from that same menu (its mean). `taker_share` is the
            # backtest's name for the live `taker_buy_share`; `p_clear` is not a
            # backtest feature (and would be in-sample if it were -- the prior
            # was fit on these rows), so `prior_top` yields nothing here.
            menu = rules["sample"](members, book)
            if menu:
                shadow = statistics.fmean(r["forward_return_pct"] for r in menu)
                by_symbol = {r["symbol"]: r["forward_return_pct"] for r in menu}
                menu_live = [{**r, "taker_buy_share": r.get("taker_share")} for r in menu]
                for arm, selector in SELECTORS.items():
                    pick = selector(menu_live)
                    if pick is None:
                        continue
                    arm_diffs[book][arm].append(by_symbol[pick] - shadow)

    out: dict = {"cross_sections_used": used, "books": {}}
    for book, per_rule in excess.items():
        pool = per_rule.get("pool", [])
        book_out = {}
        for name, vals in per_rule.items():
            row = {
                "n_cross_sections": len(vals),
                "avg_menu_size": round(statistics.fmean(sizes[book][name]), 1),
                "avg_excess_pct": round(statistics.fmean(vals), 3),
                "median_excess_pct": round(statistics.median(vals), 3),
            }
            if name != "pool" and len(vals) == len(pool):
                diffs = [m - p for m, p in zip(vals, pool, strict=True)]
                ci = bootstrap_ci(
                    diffs,
                    samples=cfg.score.bootstrap_samples,
                    seed=cfg.score.bootstrap_seed,
                    level=cfg.score.ci_level,
                )
                row["menu_minus_pool_pct"] = round(statistics.fmean(diffs), 3)
                row["ci_low"], row["ci_high"] = ci if ci else (None, None)
                row["menu_wins"] = sum(1 for d in diffs if d > 0)
            book_out[name] = row
        out["books"][book] = book_out
    # The arms leaderboard, six months deep. Sorted by the CI's lower bound
    # for the same reason the live one is.
    out["arms"] = {}
    for book, per_arm in arm_diffs.items():
        rows = []
        for arm, diffs in per_arm.items():
            ci = bootstrap_ci(
                diffs,
                samples=cfg.score.bootstrap_samples,
                seed=cfg.score.bootstrap_seed,
                level=cfg.score.ci_level,
            )
            rows.append(
                {
                    "selector": arm,
                    "n": len(diffs),
                    "edge_vs_shadow_pct": round(statistics.fmean(diffs), 3),
                    "median_pct": round(statistics.median(diffs), 3),
                    "ci_low": ci[0] if ci else None,
                    "ci_high": ci[1] if ci else None,
                    "wins": sum(1 for d in diffs if d > 0),
                }
            )
        rows.sort(key=lambda r: r["ci_low"] if r["ci_low"] is not None else -1e9, reverse=True)
        out["arms"][book] = rows
    return out


def render(result: dict) -> str:
    lines = [
        "*Screen replay* (backtest prior — not a criterion; survivorship-biased)",
        f"  {result['cross_sections_used']} historical cross-sections, 72h forward, excess vs the book's benchmark",
    ]
    for book, rules in result["books"].items():
        lines.append(f"  ── {book}")
        for name, r in rules.items():
            line = (
                f"     {name:9} menu≈{r['avg_menu_size']:>5}  excess {r['avg_excess_pct']:+6.2f}%"
                f"  median {r['median_excess_pct']:+6.2f}%  n={r['n_cross_sections']}"
            )
            if "menu_minus_pool_pct" in r:
                line += f"  vs pool {r['menu_minus_pool_pct']:+6.2f}%"
                if r.get("ci_low") is not None:
                    line += f" (CI {r['ci_low']:+.2f}..{r['ci_high']:+.2f})"
                line += f"  wins {r['menu_wins']}/{r['n_cross_sections']}"
            lines.append(line)
    for book, rows in (result.get("arms") or {}).items():
        lines.append(
            f"  ── {book} selector arms on the sample menu (pick vs a random draw from it)"
        )
        for r in rows:
            line = (
                f"     {r['selector']:12} n={r['n']:<3} edge {r['edge_vs_shadow_pct']:+6.2f}%"
                f"  median {r['median_pct']:+6.2f}%"
            )
            if r.get("ci_low") is not None:
                line += f"  CI {r['ci_low']:+.2f}..{r['ci_high']:+.2f}"
            line += f"  wins {r['wins']}/{r['n']}"
            lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argparse.ArgumentParser(
        description="Replay menu construction over the backtest corpus."
    ).parse_args(argv)
    cfg = config()
    result = replay(cfg)
    print(render(result))
    out = Path(cfg.score.screen_replay_output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
