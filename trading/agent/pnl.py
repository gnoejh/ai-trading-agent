"""Daily realised P&L, per sleeve, never pooled.

    uv run python -m trading.agent.pnl

**The owner's instruction (2026-09-13): "daily PnL for each sleeve
independantly"**, given in answer to a narrower question — should the mainnet
gate measure the MODEL's profit rather than the book's? It should, and this
module is the measurement it needs.

The gate's two profit criteria (`net P&L after costs > 0`, `avg net return per
trip > 0`) are computed over EVERY closed round trip with no attribution at
all. On 2026-09-13 both read green (+6,195 quote, +0.434%/trip) while the model
measured worse than chance, because the money was earned by the random
exploration arm and the exit contract — 54 random entries against 38 model
entries since `promotion.since`, only 11 of the model's before 09-10. A pooled
figure cannot tell you that. Pooling is the whole defect, so this module's one
rule is that nothing is ever summed across a sleeve boundary.

A **sleeve** here is a book crossed with the arm that opened the position:

    CRYPTO · model      BSTOCKS · model      KR · model
    CRYPTO · random     BSTOCKS · random     KR · random
    CRYPTO · legacy     ...

Both halves matter and for different reasons. The BOOK half is a correctness
constraint, not a preference: `pnl_quote` is denominated in the venue's own
quote currency, so adding a CRYPTO trip (USDT) to a KR trip (KRW) produces a
number in no currency at all — the same class of defect as the 2026-09-01
`/costs` bug, where USDT figures sat under KRW labels. Each sleeve therefore
carries and prints its own currency, and the report offers no grand total. The
ARM half is what answers the owner's question: it separates the money the model
earned from the money the dice earned.

**Attribution is by journal, not by guess.** Entries announce themselves —
`order` rows carry the model's intent, `explore` rows carry the random arm's
entry — so a closed trip is attributed by matching its entry timestamp to the
nearest journalled entry of the same symbol within `pnl.match_tolerance_s`. A
trip that matches nothing is `legacy` and is reported as such rather than being
silently folded into either arm: trips opened before the journalling epoch (the
fill sprint's unwind is the live example) are real money that neither arm
should be credited with. Under-claiming is the deliberate failure mode.

**Daily, because an average hides the shape.** `+0.434%/trip` over an epoch is
one number for eleven days of very different markets. A daily series shows
whether a sleeve is trending, and it is the granularity a human actually
reviews. The day is the UTC day the trip CLOSED — realised P&L belongs to the
day the money landed, and it keeps the buckets consistent with `CostLedger.day`.

This module only measures. It sets no policy and moves no capital; the gate and
the allocator remain the deciders, and flipping `use_testnet` remains the
owner's edit.
"""

from __future__ import annotations

import datetime as dt
import json

from trading.accounting.costs import CostLedger
from trading.config import AppConfig, config

# The arm that opened a position. `legacy` is not a failure of attribution so
# much as an honest category: money from before the journals could say.
MODEL = "model"
RANDOM = "random"
LEGACY = "legacy"


def _ts(value: object) -> dt.datetime | None:
    """Parse an ISO timestamp, tolerating the journal's naive rows."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def entry_events(cfg: AppConfig | None = None) -> dict[str, list[tuple[dt.datetime, str]]]:
    """Every journalled ENTRY, indexed by symbol, as (timestamp, arm).

    Both arms announce their entries in the venue's journal and they announce
    them differently, which is what makes attribution a lookup rather than an
    inference: the model's entries are `order` rows carrying a `TradeIntent`,
    the exploration arm's are `explore` rows carrying an `entry`. An `explore`
    row that was never `sent` proposed nothing and is skipped.
    """
    cfg = cfg or config()
    events: dict[str, list[tuple[dt.datetime, str]]] = {}
    for venue in cfg.score.venues:
        path = cfg.journal_for(venue)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("kind")
            if kind == "order":
                symbol = (row.get("intent") or {}).get("symbol")
                arm = MODEL
            elif kind == "explore" and row.get("sent"):
                symbol = (row.get("entry") or {}).get("symbol")
                arm = RANDOM
            else:
                continue
            when = _ts(row.get("ts"))
            if symbol and when:
                events.setdefault(str(symbol), []).append((when, arm))
    for rows in events.values():
        rows.sort()
    return events


def attribute(
    trips: list[dict],
    events: dict[str, list[tuple[dt.datetime, str]]],
    *,
    tolerance_s: float,
) -> list[dict]:
    """Tag each closed trip with the arm that opened it.

    Matching is nearest-in-time on the same symbol, within `tolerance_s`.
    Events are deliberately NOT consumed: FIFO pairing splits one buy across
    several sells, so a single journalled order legitimately fathers several
    closed trips, and consuming its event would orphan all but the first.
    """
    out = []
    for trip in trips:
        when = _ts(trip.get("entry_ts"))
        arm = LEGACY
        if when is not None:
            best = None
            for event_ts, event_arm in events.get(str(trip.get("symbol")), ()):
                gap = abs((event_ts - when).total_seconds())
                if gap <= tolerance_s and (best is None or gap < best[0]):
                    best = (gap, event_arm)
            if best is not None:
                arm = best[1]
        out.append({**trip, "arm": arm})
    return out


def _blank(currency: str) -> dict:
    return {"n": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "net_pcts": [], "currency": currency}


def _finish(bucket: dict) -> dict:
    pcts = bucket.pop("net_pcts")
    bucket["avg_net_pct"] = sum(pcts) / len(pcts) if pcts else 0.0
    return bucket


def evaluate(cfg: AppConfig | None = None, *, since: str = "") -> dict:
    """Daily and total realised P&L for every sleeve. Numbers only."""
    cfg = cfg or config()
    since = since or cfg.promotion.since or cfg.score.trade_since
    ledger = CostLedger(cfg)
    markets = set(cfg.pnl.markets or cfg.score.trade_markets)

    trips = attribute(
        ledger.closed_trades(since=since, markets=markets),
        entry_events(cfg),
        tolerance_s=cfg.pnl.match_tolerance_s,
    )

    sleeves: dict[str, dict] = {}
    # Per-ARM aggregates, pooled across books — and PERCENTAGES ONLY.
    # A net-%/trip figure is unit-free, so averaging a CRYPTO trip with a KR
    # one is meaningful where adding their quote currencies is not. This is the
    # single exception to "never cross a sleeve boundary", and it is why no
    # money figure appears here: the allocator needs one scale-free number for
    # "does this arm pick well", and that number must not be a sum.
    arm_pcts: dict[str, list[float]] = {}
    for trip in trips:
        market = str(trip.get("market") or "?")
        # Each sleeve carries its venue's own currency because these figures
        # are NEVER added together; see the module docstring.
        currency = ledger.acc.fees_for(market).currency
        key = f"{market}·{trip['arm']}"
        sleeve = sleeves.setdefault(
            key,
            {
                "market": market,
                "arm": trip["arm"],
                "currency": currency,
                "days": {},
                "total": _blank(currency),
            },
        )
        closed_at = _ts(trip.get("exit_ts"))
        day = closed_at.astimezone(dt.UTC).date().isoformat() if closed_at else "unknown"
        hurdle = ledger.breakeven_move_pct(market)
        fee = trip["quantity"] * trip["entry_price"] * hurdle
        net_pct = trip["return_pct"] - hurdle * 100
        for bucket in (sleeve["days"].setdefault(day, _blank(currency)), sleeve["total"]):
            bucket["n"] += 1
            bucket["gross"] += trip["pnl_quote"]
            bucket["fees"] += fee
            bucket["net"] += trip["pnl_quote"] - fee
            bucket["net_pcts"].append(net_pct)
        arm_pcts.setdefault(trip["arm"], []).append(net_pct)

    for sleeve in sleeves.values():
        sleeve["days"] = {d: _finish(b) for d, b in sorted(sleeve["days"].items())}
        sleeve["total"] = _finish(sleeve["total"])
    arms = {
        arm: {"n": len(pcts), "avg_net_pct": sum(pcts) / len(pcts)}
        for arm, pcts in arm_pcts.items()
    }
    return {"since": since, "n_trips": len(trips), "sleeves": sleeves, "arms": arms}


def render(cfg: AppConfig | None = None, *, since: str = "", days: int = 0) -> str:
    """The per-sleeve report. No grand total exists, by design."""
    cfg = cfg or config()
    result = evaluate(cfg, since=since)
    days = days or cfg.pnl.days
    lines = [
        "*Daily P&L per sleeve* (each sleeve on its own money — never pooled)",
        f"  since {result['since']}, {result['n_trips']} closed trips",
    ]
    if not result["sleeves"]:
        lines.append("  no closed trips in this epoch yet")
        return "\n".join(lines)

    # Model sleeves first: they are the ones the mainnet verdict turns on.
    order = {MODEL: 0, RANDOM: 1, LEGACY: 2}
    for key in sorted(
        result["sleeves"], key=lambda k: (order.get(result["sleeves"][k]["arm"], 9), k)
    ):
        sleeve = result["sleeves"][key]
        total = sleeve["total"]
        mark = "✅" if total["net"] > 0 else "🔻"
        lines.append(f"  ── {sleeve['market']} · {sleeve['arm']} ({sleeve['currency']})")
        recent = list(sleeve["days"].items())
        if days:
            recent = recent[-days:]
        for day, bucket in recent:
            lines.append(
                f"     {day}   n={bucket['n']:>3}   net {bucket['net']:>+12,.2f}"
                f"   ({bucket['avg_net_pct']:+.2f}%/trip)"
            )
        lines.append(
            f"     {mark} total  n={total['n']:>3}   net {total['net']:>+12,.2f}"
            f"   ({total['avg_net_pct']:+.2f}%/trip,"
            f" {total['gross']:+,.2f} gross − {total['fees']:,.2f} fees)"
        )
    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Daily realised P&L per sleeve")
    ap.add_argument("--since", default="", help="epoch start (default: promotion.since)")
    ap.add_argument("--days", type=int, default=0, help="daily rows to show (default: pnl.days)")
    ap.add_argument("--json", action="store_true", help="emit the raw measurement")
    args = ap.parse_args()
    if args.json:
        print(json.dumps(evaluate(since=args.since), indent=2, ensure_ascii=False))
    else:
        print(render(since=args.since, days=args.days))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
