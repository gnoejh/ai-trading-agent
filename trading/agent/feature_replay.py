"""Which features are information at the 72h horizon? Six months says.

    uv run python -m trading.agent.feature_replay

The richer feature set (`features.py`) is validated here BEFORE it reaches the
prompt, three ways, all on the backtest cross-sections joined with
`score.backtest_features`:

1. DECILE SPREADS — the 2026-08-10 methodology. Every observation's forward
   excess return (vs its section's BTC) is bucketed by the feature's decile;
   the spread is top minus bottom, with a bootstrap CI. This is the test flow
   passed at +1.05% on daily bars, and the one momentum failed.
2. PICK ARMS — each feature as a top-pick and bottom-pick selector on the
   `sample` menu, paired against a random draw from that menu. This is the
   test flow FAILED (2026-09-16): a decile spread and "the top name is the
   best name" are different claims, and only the second is what a selector
   does.
3. REGIME — the pool's forward return (raw, and excess vs BTC) conditioned on
   the market state: BTC's trailing week and breadth. Long-only with no regime
   filter is beta with costs; this asks whether the state is worth knowing.

A PRIOR by the repo's rule, with the backfill's survivorship bias. But it is
the difference between "give the model more data" and "give the model data
that measured as information".
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import defaultdict
from pathlib import Path

from trading.agent.features import PATH_KEYS, RS_KEYS
from trading.agent.scorer import SELECTORS, bootstrap_ci
from trading.agent.screen_replay import _sample_menu, load_cross_sections
from trading.config import AppConfig, config

log = logging.getLogger(__name__)

FUNDING_KEYS = ["funding_rate_pct", "funding_3d_avg_pct"]
FEATURES = (
    [k for k in PATH_KEYS if k != "ret_24h"] + RS_KEYS + FUNDING_KEYS
)  # ret_24h == change_pct, already an arm

# The equity venues replay on DAILY features (features.daily_path_features),
# their own benchmark, and a 3-trading-day horizon -- the backtest_kr/us
# corpora were resolved that way. One spec per venue keeps the pipeline the
# same and the labels honest.
from trading.agent.features import DAILY_PATH_KEYS, DAILY_RS_KEYS

VENUES = {
    "CRYPTO": {
        "book": "CRYPTO",
        "source": "backtest",
        "benchmark": "BTCUSDT",
        "features": FEATURES,
        "horizon": "72h",
        "side_files": ("backtest_features", "backtest_funding"),
    },
    "KR": {
        "book": "KR",
        "source": "backtest_kr",
        "benchmark": "069500",
        "features": [k for k in DAILY_PATH_KEYS if k != "ret_1d"] + DAILY_RS_KEYS,
        "horizon": "3 trading days",
        "side_files": ("by_venue:KR",),
    },
    "US": {
        "book": "US",
        "source": "backtest_us",
        "benchmark": "SPY",
        "features": [k for k in DAILY_PATH_KEYS if k != "ret_1d"] + DAILY_RS_KEYS,
        "horizon": "3 trading days",
        "side_files": ("by_venue:US",),
    },
}


def _side_file(cfg: AppConfig, name: str) -> Path:
    if name.startswith("by_venue:"):
        return Path(cfg.score.backtest_features_by_venue[name.split(":", 1)[1]])
    return Path(getattr(cfg.score, name))


def load_features(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[r["id"]] = r
    return out


def _sections(cfg: AppConfig, venue: str = "CRYPTO"):
    spec = VENUES[venue]
    groups = load_cross_sections(Path(cfg.score.observations), source=spec["source"])
    # Every side-file joins by observation id; a row absent from one simply
    # lacks those keys (a spot name without a perp, a name without bars).
    sides = [load_features(_side_file(cfg, name)) for name in spec["side_files"]]
    min_group = cfg.score.screen_replay_min_group
    anchor_key = spec["features"][0]
    out = []
    for ts, rows in sorted(groups.items()):
        members = []
        for r in rows:
            if str(r.get("book") or spec["book"]) != spec["book"]:
                continue
            m = dict(r)
            for side in sides:
                m.update(
                    {
                        k: v
                        for k, v in side.get(r["id"], {}).items()
                        if k not in ("id", "symbol", "ts")
                    }
                )
            members.append(m)
        bench = next(
            (
                r
                for r in members
                if r["symbol"] == spec["benchmark"] and r.get(anchor_key) is not None
            ),
            None,
        )
        if bench is None or len(members) < min_group:
            continue
        out.append((ts, members, bench))
    return out


def deciles(sections, feature: str, cfg: AppConfig) -> dict | None:
    rows = []
    for _ts, members, bench in sections:
        b = bench["forward_return_pct"]
        for r in members:
            v = r.get(feature)
            if v is not None:
                rows.append((float(v), r["forward_return_pct"] - b))
    if len(rows) < 200:
        return None
    rows.sort()
    k = len(rows) // 10
    top = [e for _, e in rows[-k:]]
    bottom = [e for _, e in rows[:k]]
    spread = statistics.fmean(top) - statistics.fmean(bottom)
    # Bootstrap the spread by resampling within each decile.
    import random

    rng = random.Random(cfg.score.bootstrap_seed)
    draws = []
    for _ in range(min(cfg.score.bootstrap_samples, 1000)):
        t = statistics.fmean(rng.choice(top) for _ in range(len(top)))
        bm = statistics.fmean(rng.choice(bottom) for _ in range(len(bottom)))
        draws.append(t - bm)
    draws.sort()
    lo = draws[int(0.025 * len(draws))]
    hi = draws[int(0.975 * len(draws)) - 1]
    return {
        "feature": feature,
        "n": len(rows),
        "top_decile_excess_pct": round(statistics.fmean(top), 3),
        "bottom_decile_excess_pct": round(statistics.fmean(bottom), 3),
        "spread_pct": round(spread, 3),
        "ci_low": round(lo, 3),
        "ci_high": round(hi, 3),
    }


def pick_arms(sections, cfg: AppConfig, venue: str = "CRYPTO") -> list[dict]:
    diffs: dict[str, list[float]] = defaultdict(list)
    slots = cfg.agent.screen.book_slots.get(venue, cfg.agent.screen.candidates)
    for _ts, members, _bench in sections:
        menu = _sample_menu(members, slots)
        if not menu:
            continue
        shadow = statistics.fmean(r["forward_return_pct"] for r in menu)
        by_symbol = {r["symbol"]: r["forward_return_pct"] for r in menu}
        live = [{**r, "taker_buy_share": r.get("taker_share")} for r in menu]
        for name, selector in SELECTORS.items():
            pick = selector(live)
            if pick is not None:
                diffs[name].append(by_symbol[pick] - shadow)
    out = []
    for name, d in diffs.items():
        ci = bootstrap_ci(
            d,
            samples=cfg.score.bootstrap_samples,
            seed=cfg.score.bootstrap_seed,
            level=cfg.score.ci_level,
        )
        out.append(
            {
                "selector": name,
                "n": len(d),
                "edge_pct": round(statistics.fmean(d), 3),
                "median_pct": round(statistics.median(d), 3),
                "ci_low": ci[0] if ci else None,
                "ci_high": ci[1] if ci else None,
                "wins": sum(1 for x in d if x > 0),
            }
        )
    out.sort(key=lambda r: r["ci_low"] if r["ci_low"] is not None else -1e9, reverse=True)
    return out


def regime_table(sections, trend_key: str = "ret_7d") -> list[dict]:
    """The pool's forward return by market state at the section's open."""
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for _ts, members, bench in sections:
        raw = statistics.fmean(r["forward_return_pct"] for r in members)
        excess = raw - bench["forward_return_pct"]
        r7 = bench.get(trend_key)
        up = [r.get(trend_key) for r in members if r.get(trend_key) is not None]
        breadth = (sum(1 for v in up if v > 0) / len(up)) if up else None
        buckets["all"].append((raw, excess))
        if r7 is not None:
            buckets["btc_7d_up" if r7 > 0 else "btc_7d_down"].append((raw, excess))
        if breadth is not None:
            buckets["breadth_7d>0.5" if breadth > 0.5 else "breadth_7d<=0.5"].append((raw, excess))
    out = []
    for name, vals in buckets.items():
        out.append(
            {
                "state": name,
                "n_sections": len(vals),
                "pool_raw_pct": round(statistics.fmean(v[0] for v in vals), 3),
                "pool_excess_pct": round(statistics.fmean(v[1] for v in vals), 3),
                "median_raw_pct": round(statistics.median(v[0] for v in vals), 3),
            }
        )
    # Is the up/down split real? Bootstrap the difference of the two groups'
    # mean RAW return (raw, because a long-only book earns beta).
    up = [v[0] for v in buckets.get("btc_7d_up", [])]
    down = [v[0] for v in buckets.get("btc_7d_down", [])]
    if len(up) > 5 and len(down) > 5:
        import random

        rng = random.Random(20260916)
        draws = sorted(
            statistics.fmean(rng.choice(up) for _ in up)
            - statistics.fmean(rng.choice(down) for _ in down)
            for _ in range(1000)
        )
        out.append(
            {
                "state": "up_minus_down",
                "n_sections": len(up) + len(down),
                "pool_raw_pct": round(statistics.fmean(up) - statistics.fmean(down), 3),
                "pool_excess_pct": None,
                "median_raw_pct": None,
                "ci_low": round(draws[25], 3),
                "ci_high": round(draws[974], 3),
            }
        )
    return out


def menu_rules(sections, cfg: AppConfig, venue: str = "CRYPTO") -> list[dict]:
    """Menus built from the decile result, each vs the unfiltered pool, paired.

    The decile spread is a PORTFOLIO effect: a random draw from the top of the
    weekly range beats one from the bottom. The actionable form is a menu that
    drops the measured losers and samples across the rest -- tested here the
    way the screen control tests a menu live: random draw from the menu minus
    random draw from the pool, per section, with a CI.
    """
    slots = cfg.agent.screen.book_slots.get(venue, cfg.agent.screen.candidates)
    rules = {
        "sample": lambda m: m,
        "range>=0.2": lambda m: [r for r in m if (r.get("range_pos_7d") or 0) >= 0.2],
        "range>=0.5": lambda m: [r for r in m if (r.get("range_pos_7d") or 0) >= 0.5],
        "range>=0.8": lambda m: [r for r in m if (r.get("range_pos_7d") or 0) >= 0.8],
        "ret72h>0": lambda m: [r for r in m if (r.get("ret_72h") or 0) > 0],
        # Positioning: the bottom funding decile loses and the contrarian pick
        # loses significantly, so the actionable form is an EXCLUSION -- drop
        # names whose longs are being paid to hold (negative funding), keep
        # names without a perp (no reading is not a bad reading).
        "funding>=0": lambda m: [
            r for r in m if r.get("funding_rate_pct") is None or r["funding_rate_pct"] >= 0
        ],
        "fund>=0&range>=0.5": lambda m: [
            r
            for r in m
            if (r.get("funding_rate_pct") is None or r["funding_rate_pct"] >= 0)
            and (r.get("range_pos_7d") or 0) >= 0.5
        ],
    }
    diffs: dict[str, list[float]] = defaultdict(list)
    sizes: dict[str, list[int]] = defaultdict(list)
    for _ts, members, _bench in sections:
        pool_mean = statistics.fmean(r["forward_return_pct"] for r in members)
        for name, rule in rules.items():
            kept = rule(members)
            menu = _sample_menu(kept, slots)
            if len(menu) < 5:
                continue
            diffs[name].append(statistics.fmean(r["forward_return_pct"] for r in menu) - pool_mean)
            sizes[name].append(len(kept))
    out = []
    for name, d in diffs.items():
        ci = bootstrap_ci(
            d,
            samples=cfg.score.bootstrap_samples,
            seed=cfg.score.bootstrap_seed,
            level=cfg.score.ci_level,
        )
        out.append(
            {
                "rule": name,
                "n": len(d),
                "avg_pool_after_filter": round(statistics.fmean(sizes[name]), 1),
                "menu_minus_pool_pct": round(statistics.fmean(d), 3),
                "median_pct": round(statistics.median(d), 3),
                "ci_low": ci[0] if ci else None,
                "ci_high": ci[1] if ci else None,
                "wins": sum(1 for x in d if x > 0),
            }
        )
    out.sort(key=lambda r: r["ci_low"] if r["ci_low"] is not None else -1e9, reverse=True)
    return out


def replay(cfg: AppConfig | None = None, venue: str = "CRYPTO") -> dict:
    cfg = cfg or config()
    spec = VENUES[venue]
    sections = _sections(cfg, venue)
    anchor = spec["features"][0]
    with_feats = sum(1 for _, m, _ in sections if any(r.get(anchor) is not None for r in m))
    dec = [d for f in spec["features"] if (d := deciles(sections, f, cfg))]
    dec.sort(key=lambda d: d["ci_low"], reverse=True)
    trend = "ret_7d" if venue == "CRYPTO" else "ret_5d"
    return {
        "venue": venue,
        "horizon": spec["horizon"],
        "sections": len(sections),
        "sections_with_features": with_feats,
        "deciles": dec,
        "pick_arms": pick_arms(sections, cfg, venue),
        "menu_rules": menu_rules(sections, cfg, venue),
        "regime": regime_table(sections, trend),
    }


def render(result: dict) -> str:
    lines = [
        "*Feature replay* (backtest prior — not a criterion; survivorship-biased)",
        (
            f"  {result['sections']} cross-sections ({result['sections_with_features']} with features), "
            f"{result.get('horizon', '72h')} forward, {result.get('venue', 'CRYPTO')}"
        ),
        "  ── decile spreads: top 10% minus bottom 10% of each feature, excess vs BTC",
    ]
    for d in result["deciles"]:
        mark = " <- excludes 0" if d["ci_low"] > 0 or d["ci_high"] < 0 else ""
        lines.append(
            f"     {d['feature']:18} n={d['n']:<6} top {d['top_decile_excess_pct']:+6.2f}%  "
            f"bottom {d['bottom_decile_excess_pct']:+6.2f}%  spread {d['spread_pct']:+6.2f}%  "
            f"CI {d['ci_low']:+.2f}..{d['ci_high']:+.2f}{mark}"
        )
    lines.append("  ── pick arms on the sample menu (pick vs a random draw from it)")
    for r in result["pick_arms"]:
        ci = f"  CI {r['ci_low']:+.2f}..{r['ci_high']:+.2f}" if r["ci_low"] is not None else ""
        mark = (
            " <- excludes 0"
            if r["ci_low"] is not None and (r["ci_low"] > 0 or r["ci_high"] < 0)
            else ""
        )
        lines.append(
            f"     {r['selector']:18} n={r['n']:<4} edge {r['edge_pct']:+6.2f}%  median {r['median_pct']:+6.2f}%{ci}  wins {r['wins']}/{r['n']}{mark}"
        )
    lines.append("  ── menus from the decile result: random draw from the menu vs from the pool")
    for r in result.get("menu_rules", []):
        ci = f"  CI {r['ci_low']:+.2f}..{r['ci_high']:+.2f}" if r["ci_low"] is not None else ""
        mark = (
            " <- excludes 0"
            if r["ci_low"] is not None and (r["ci_low"] > 0 or r["ci_high"] < 0)
            else ""
        )
        lines.append(
            f"     {r['rule']:12} n={r['n']:<3} pool→{r['avg_pool_after_filter']:>6}  "
            f"vs pool {r['menu_minus_pool_pct']:+6.2f}%  median {r['median_pct']:+6.2f}%{ci}"
            f"  wins {r['wins']}/{r['n']}{mark}"
        )
    lines.append("  ── regime: the pool's 72h forward return by market state at entry")
    for r in result["regime"]:
        if r["state"] == "up_minus_down":
            lines.append(
                f"     {r['state']:16} n={r['n_sections']:<3} diff {r['pool_raw_pct']:+6.2f}%  "
                f"CI {r['ci_low']:+.2f}..{r['ci_high']:+.2f}"
                + (" <- excludes 0" if r["ci_low"] > 0 else "")
            )
            continue
        lines.append(
            f"     {r['state']:16} n={r['n_sections']:<3} raw {r['pool_raw_pct']:+6.2f}%  "
            f"median {r['median_raw_pct']:+6.2f}%  excess vs BTC {r['pool_excess_pct']:+6.2f}%"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Validate the feature set over the backtest corpus.")
    ap.add_argument("--venue", default="CRYPTO", choices=sorted(VENUES))
    args = ap.parse_args(argv)
    cfg = config()
    result = replay(cfg, args.venue)
    print(render(result))
    out = Path(cfg.score.feature_replay_output)
    if args.venue != "CRYPTO":
        out = out.with_name(f"{out.stem}_{args.venue.lower()}{out.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
