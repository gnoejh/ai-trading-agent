"""Historical decision replay — model-vs-random on reconstructed menus.

    uv run python -m trading.agent.replay --decisions 50

The mainnet gate's slowest question is "does the model's selection beat a
random pick from the same menu?" Live, each answer costs a decision cycle plus
a 72-hour wait. Here the menu is reconstructed at a HISTORICAL moment — same
screen logic, same decide prompt, same trade-rules contract — the model picks,
a seeded random picks from the identical menu, and both resolve instantly
against the klines that followed.

Integrity rules, enforced by construction:

* **No lookahead.** Menu features come from `features_at`, whose slice is
  structurally bounded at the decision bar; resolution reads only later bars.
* **Provenance separated.** Results land in `data/replay.jsonl` and
  `data/replay_summary.json` — never in the live experience store. The
  promotion gate shows the summary as a PRIOR, not a criterion: live pairs
  still decide promotion.
* **Survivorship, accepted and documented:** the pool is today's universe, so
  mid-window delistings are absent. Optimistic bias; live confirms.

**This bills real DeepSeek tokens through the shared ledger**, so it competes
with the live loop's daily API budget (`accounting.max_api_krw_per_day`) — the
decide loop pauses when the budget is spent. The `--decisions` cap is the
throttle; ~12 KRW per decision at the flash tier.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from pathlib import Path
from types import SimpleNamespace

from trading.accounting.costs import CostLedger
from trading.agent.backfill import FLOW_HOURS, features_at, fetch_hourly, resolve_forward
from trading.agent.loop import _SYSTEM, TradingAgent, _describe_limits, build_trade_rules
from trading.config import AppConfig, config
from trading.llm.client import LLMClient

log = logging.getLogger(__name__)

MS_PER_HOUR = 3_600_000


class SymbolHistory:
    """One symbol's bars plus an open-time index, so cross-symbol alignment is
    by TIMESTAMP. Aligning by bar index looked equivalent and was not: a coin
    listed mid-window starts its array later, so index i meant a different
    wall-clock moment per symbol — features from different times in one menu.
    """

    def __init__(self, book: str, bars: list[list]):
        self.book = book
        self.bars = bars
        self.at = {int(b[0]): i for i, b in enumerate(bars)}


def build_menu(histories: dict[str, SymbolHistory], t_ms: int, cfg) -> list[dict]:
    """The candidate menu as the screen would have built it at time `t_ms`.

    Same strategy bounds (min/max 24h change) and the same flow ranking per
    book, using only bars at or before the decision moment.
    """
    scfg = cfg.agent.screen
    rows = []
    for symbol, h in histories.items():
        i = h.at.get(t_ms)
        if i is None:
            continue
        f = features_at(h.bars, i)
        if f is None:
            continue
        chg = f["change_pct"]
        if scfg.min_change_pct and chg < scfg.min_change_pct * 100:
            continue
        if scfg.max_change_pct and chg > scfg.max_change_pct * 100:
            continue
        rows.append({"symbol": symbol, "book": h.book, **f})
    menu: list[dict] = []
    by_book: dict[str, list[dict]] = {}
    for r in rows:
        by_book.setdefault(r["book"], []).append(r)
    for book, members in by_book.items():
        members.sort(key=lambda r: -(r.get("taker_share") or 0))
        menu.extend(members[: scfg.book_slots.get(book, scfg.candidates)])
    return menu


class Replayer:
    def __init__(self, client, screen, cfg: AppConfig | None = None, llm=None, rng=None):
        self.cfg = cfg or config()
        self.client = client  # data plane: mainnet by construction
        self.screen = screen
        self.ledger = CostLedger(self.cfg)
        self.llm = llm or LLMClient(self.cfg, ledger=self.ledger)
        self.rng = rng or random.Random(self.cfg.explore.seed or None)
        self.out_path = Path(self.cfg.score.replay)
        self.summary_path = Path(self.cfg.score.replay_summary)

    def _payload(self, menu: list[dict]) -> str:
        return json.dumps(
            {
                "trade_rules": build_trade_rules(self.cfg, "BINANCE", self.ledger),
                "limits": _describe_limits(self.cfg.risk),
                "candidates": menu,
                "cash": {"note": "historical replay — selection is measured, nothing is traded"},
                "holdings": {},
                "unmanaged_balances": 0,
                "open_orders": {},
            },
            ensure_ascii=False,
            default=str,
        )[:20000]

    def run(
        self,
        decisions: int,
        days: int | None = None,
        symbols: int = 300,
        min_menu: int = 4,
    ) -> dict:
        days = days or self.cfg.score.backfill_days
        horizon_h = max(int(self.cfg.score.horizon_minutes // 60), 1)
        end = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        start_ms = int((end - dt.timedelta(days=days)).timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        # Deepest names first: a menu needs candidates, and thin names rarely
        # pass the change bounds anyway. `symbols` caps the kline downloads.
        pool = sorted(
            self.screen.tradable_pool(0.0), key=lambda e: -float(e.get("quote_volume") or 0)
        )[:symbols]
        histories: dict[str, SymbolHistory] = {}
        for entry in pool:
            try:
                bars = fetch_hourly(self.client, entry["symbol"], start_ms, end_ms)
            except Exception as exc:  # noqa: BLE001 - one dead symbol must not stall the rest
                log.warning("replay klines %s failed: %s", entry["symbol"], exc)
                continue
            if len(bars) > FLOW_HOURS + horizon_h:
                histories[entry["symbol"]] = SymbolHistory(entry["book"], bars)
        if not histories:
            return {"decisions": 0, "note": "no usable history"}

        # Decision MOMENTS on the shared wall clock, one horizon apart at
        # minimum so paired outcomes stay non-overlapping. Each symbol joins a
        # moment only if it has a bar exactly there (build_menu aligns by
        # timestamp, so late listings simply sit out their pre-listing moments).
        first_ms = start_ms + FLOW_HOURS * MS_PER_HOUR
        last_ms = end_ms - (horizon_h + 1) * MS_PER_HOUR
        if last_ms <= first_ms:
            return {"decisions": 0, "note": "window shorter than flow lookback + horizon"}
        span = last_ms - first_ms
        # Whole hours only: a moment that is not a bar's open time matches no
        # symbol's timestamp index and silently yields an empty menu.
        step_ms = max(
            (span // max(decisions, 1)) // MS_PER_HOUR * MS_PER_HOUR,
            horizon_h * MS_PER_HOUR,
        )
        moments = list(range(first_ms, last_ms, step_ms))[:decisions]

        results = []
        with self.out_path.open("a", encoding="utf-8") as fh:
            for t_ms in moments:
                menu = build_menu(histories, t_ms, self.cfg)
                if len(menu) < min_menu:
                    # A pick from a near-empty menu measures nothing: with two
                    # names, half of all pairs are forced ties.
                    continue
                raw = self.llm.ask(
                    self._payload(menu), system=_SYSTEM, tier=self.cfg.agent.tiers.decide
                )
                stub = SimpleNamespace(market="BINANCE")
                intents, _, best = TradingAgent._parse(stub, raw, {m["symbol"] for m in menu}, {})
                model_pick = best or next((x.symbol for x in intents if str(x.side) == "BUY"), None)
                shadow_pick = self.rng.choice(menu)["symbol"]
                if not model_pick:
                    continue

                def fwd(symbol, t=t_ms):
                    h = histories[symbol]
                    i = h.at[t]
                    r = resolve_forward(h.bars, i, horizon_h, float(h.bars[i][4]))
                    return r["forward_return_pct"] if r else None

                m_ret, s_ret = fwd(model_pick), fwd(shadow_pick)
                if m_ret is None or s_ret is None:
                    continue
                rec = {
                    "ts": dt.datetime.fromtimestamp(t_ms / 1000, dt.UTC).isoformat(),
                    "menu_size": len(menu),
                    "model": model_pick,
                    "model_return_pct": m_ret,
                    "shadow": shadow_pick,
                    "shadow_return_pct": s_ret,
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                results.append(rec)

        summary = self._summarise()
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        log.info("replay: %s", summary)
        return summary

    def _summarise(self) -> dict:
        """Aggregate EVERY replay record ever written, not just this run's."""
        rows = []
        if self.out_path.exists():
            with self.out_path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        n = len(rows)
        # A tie (shadow drew the model's own pick) is a legitimate draw from
        # the menu but carries no information about the difference; the win
        # rate is only meaningful over decided pairs.
        ties = sum(1 for r in rows if r["model"] == r["shadow"])
        return {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "n": n,
            "ties": ties,
            "model_avg_pct": round(sum(r["model_return_pct"] for r in rows) / n, 3) if n else None,
            "shadow_avg_pct": round(sum(r["shadow_return_pct"] for r in rows) / n, 3)
            if n
            else None,
            "model_wins": sum(1 for r in rows if r["model_return_pct"] > r["shadow_return_pct"]),
            "note": "backtest PRIOR (survivorship-biased); live pairs decide promotion",
        }


def main() -> int:
    import argparse

    from trading.brokers.adapters import build_adapter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="binance", choices=["binance"])
    ap.add_argument("--decisions", type=int, default=50, help="hard cap on billed model calls")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--symbols", type=int, default=300, help="deepest N names to download")
    ap.add_argument(
        "--min-menu", type=int, default=4, help="skip moments with fewer qualifying candidates"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    adapter = build_adapter(args.broker, None, cfg)
    summary = Replayer(adapter.client, adapter.screen, cfg).run(
        decisions=args.decisions, days=args.days, symbols=args.symbols, min_menu=args.min_menu
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
