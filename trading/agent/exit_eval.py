"""Counterfactual evaluation of the exit contract — the outer loop's exit gradient.

    uv run python -m trading.agent.exit_eval
    uv run python -m trading.agent.exit_eval --since 2026-09-02

The learning loop learns ENTRIES. Exits are fixed by the hurdle formula, and
the fill sprint showed they set the per-trip P&L (180-minute holds bled ~0.4%
a trip on terms that never counted). This module replays every closed trade's
own price path — the venue's price record, already fetched for resolution —
under a grid of holds and stops, using the SAME `ExitPolicy` arithmetic the
supervisor runs (stop, hurdle-derived target, trail armed past net
break-even, time stop). Offline policy evaluation at zero cost: no order, no
token, no waiting.

What it cannot do: change anything. It writes `exit_eval.json` and prints the
grid; the owner reads it and edits `exits` in config.yaml with the reasoning
committed — a config gradient step, never an automatic one.

Approximations, stated: the live supervisor samples price every cycle; the
replay sees hourly bars, so a stop is triggered by the bar's low and a target
by its high (a bar touching both counts as a stop), and the trail ratchets on
bar closes. Fees are charged at each venue's round-trip hurdle, as the gate
does.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.agent.prices import Window, price_source
from trading.config import AppConfig, MarketExits, config
from trading.risk.exits import ExitPolicy

log = logging.getLogger(__name__)


def _parse_ts(value: str) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def policy_for(cfg: AppConfig, market: str, *, stop_pct: float, hold_minutes: float) -> ExitPolicy:
    """An ExitPolicy for `market` with the stop and hold overridden.

    Built on a config COPY: everything else (target multiple, reward:risk,
    trail) stays exactly what the live supervisor uses, so the grid varies
    two knobs and nothing else.
    """
    mcfg = cfg.model_copy(deep=True)
    base = mcfg.exits.markets.get(market) or MarketExits()
    mcfg.exits.markets[market] = base.model_copy(
        update={"stop_loss_pct": stop_pct, "max_hold_minutes": hold_minutes}
    )
    return ExitPolicy(mcfg, CostLedger(mcfg), market=market)


def simulate(
    window: Window, entry: float, opened: dt.datetime, policy: ExitPolicy, hold_minutes: float
) -> dict:
    """Replay one trip under `policy`. Returns exit price, reason and minutes held."""
    plan = policy.plan_for("replay", entry, 1.0, opened_at=opened.isoformat())
    opened_ms = int(opened.timestamp() * 1000)
    last_close = entry
    for bar in window.bars:
        held_min = (bar.t - opened_ms) / 60_000
        if bar.low <= plan.stop:
            return {"exit_price": plan.stop, "reason": "stop", "minutes": held_min}
        if bar.high >= plan.target:
            return {"exit_price": plan.target, "reason": "target", "minutes": held_min}
        last_close = bar.close
        plan.high_water = max(plan.high_water, bar.close)
        trail = policy.trail_stop_for(plan, bar.close)
        if trail is not None:
            plan.tighten_stop(trail)
        if hold_minutes and held_min >= hold_minutes:
            return {"exit_price": bar.close, "reason": "time", "minutes": held_min}
    return {
        "exit_price": last_close,
        "reason": "time" if window.complete else "open",
        "minutes": (window.bars[-1].t - opened_ms) / 60_000 if window.bars else 0.0,
    }


class ExitEvaluator:
    def __init__(self, cfg: AppConfig | None = None, client=None):
        self.cfg = cfg or config()
        self.ecfg = self.cfg.exit_eval
        self.ledger = CostLedger(self.cfg)
        self.client = client
        self._sources: dict[str, object] = {}

    def _source(self, venue: str):
        if venue not in self._sources:
            self._sources[venue] = price_source(venue, self.cfg, self.client)
        return self._sources[venue]

    @staticmethod
    def _venue(market: str) -> str:
        return "BINANCE" if market in ("CRYPTO", "BSTOCKS", "BINANCE") else market

    def run(self, since: str = "") -> dict:
        since = since or self.cfg.promotion.since or self.cfg.score.trade_since
        trades = self.ledger.closed_trades(since=since)
        max_hold = max(self.ecfg.holds_minutes or [self.cfg.exits.max_hold_minutes])
        cells: dict[tuple[int, float], list[dict]] = {
            (h, s): [] for h in self.ecfg.holds_minutes for s in self.ecfg.stops_pct
        }
        actual: list[float] = []
        replayed = skipped = 0
        for t in trades:
            opened = _parse_ts(t.get("entry_ts", ""))
            if opened is None or t["entry_price"] <= 0:
                skipped += 1
                continue
            venue = self._venue(t["market"])
            source = self._source(venue)
            if source is None:
                skipped += 1
                continue
            window = source.window(t["symbol"], opened, opened + dt.timedelta(minutes=max_hold))
            if window is None or not window.bars:
                skipped += 1
                continue
            hurdle = self.ledger.breakeven_move_pct(t["market"]) * 100
            actual.append(t["return_pct"] - hurdle)
            replayed += 1
            for (hold, stop), members in cells.items():
                policy = policy_for(self.cfg, venue, stop_pct=stop, hold_minutes=hold)
                sim = simulate(window, t["entry_price"], opened, policy, hold)
                members.append(
                    {
                        "net_pct": (sim["exit_price"] / t["entry_price"] - 1) * 100 - hurdle,
                        "reason": sim["reason"],
                    }
                )

        def summarise(rows: list[dict]) -> dict:
            nets = [r["net_pct"] for r in rows]
            reasons = {}
            for r in rows:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            return {
                "n": len(nets),
                "avg_net_pct": round(sum(nets) / len(nets), 3) if nets else None,
                "median_net_pct": round(statistics.median(nets), 3) if nets else None,
                "win_rate": round(sum(1 for v in nets if v > 0) / len(nets), 3) if nets else None,
                "exits": reasons,
            }

        grid = [
            {"hold_minutes": h, "stop_pct": s, **summarise(rows)} for (h, s), rows in cells.items()
        ]
        result = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "since": since,
            "replayed": replayed,
            "skipped": skipped,
            "actual": {
                "n": len(actual),
                "avg_net_pct": round(sum(actual) / len(actual), 3) if actual else None,
                "median_net_pct": round(statistics.median(actual), 3) if actual else None,
            },
            "live_contract": {
                m: {
                    "stop_pct": self.cfg.exits.for_market(m).stop_loss_pct,
                    "hold_minutes": self.cfg.exits.for_market(m).max_hold_minutes,
                }
                for m in ("BINANCE", "KR", "US")
            },
            "grid": grid,
        }
        out = Path(self.ecfg.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result


def render(result: dict) -> str:
    lines = [
        (
            f"exit counterfactuals since {result['since'] or 'the beginning'}: "
            f"{result['replayed']} trips replayed, {result['skipped']} skipped"
        ),
        (
            f"actual contract: avg {result['actual']['avg_net_pct']}% net, "
            f"median {result['actual']['median_net_pct']}% (n={result['actual']['n']})"
        ),
        f"{'hold':>8} {'stop':>6} {'n':>5} {'avg net%':>9} {'median%':>8} {'win':>5}  exits",
    ]
    for g in result["grid"]:
        lines.append(
            f"{g['hold_minutes']:>8} {g['stop_pct']:>6.2%} {g['n']:>5} "
            f"{g['avg_net_pct'] if g['avg_net_pct'] is not None else '-':>9} "
            f"{g['median_net_pct'] if g['median_net_pct'] is not None else '-':>8} "
            f"{g['win_rate'] if g['win_rate'] is not None else '-':>5}  {g['exits']}"
        )
    return "\n".join(lines)


def main() -> int:
    import argparse

    from trading.brokers.adapters import build_adapter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="", help="ISO date; default promotion.since")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config()
    client = build_adapter("binance", None, cfg).client
    result = ExitEvaluator(cfg, client).run(since=args.since)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
