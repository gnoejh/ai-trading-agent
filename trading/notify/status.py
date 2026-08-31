"""Render broker account state as a Telegram status report.

Everything here reads from the broker-backed account state, so a status report
can never show locally cached fiction.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.config import AppConfig
from trading.risk.exits import ExitPlan, exit_state_path

log = logging.getLogger(__name__)


def _flt(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _hours_since(iso: str, now: dt.datetime) -> float:
    try:
        opened = dt.datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.UTC)
    return (now - opened).total_seconds() / 3600


class BinanceStatusReporter:
    """The operator's only window into the Binance account.

    The Binance app cannot display a testnet account, so this report has to
    carry both what the app would have shown (cash, marks, open orders) and
    what it never could: each managed position's committed exit plan — stop,
    net break-even, target — read from the supervisor's persisted state. The
    testnet's ~480 seed balances have no cost basis and are never managed, so
    they collapse to one summary line instead of drowning the report.
    """

    def __init__(self, state, cfg: AppConfig, market: str = "BINANCE", ledger=None):
        self.state = state
        self.cfg = cfg
        self.plans_path = exit_state_path(cfg, market)
        self.ledger = ledger or CostLedger(cfg)

    def safe_report(self, **sections) -> str:
        """Never raise into the chat loop -- report the failure instead."""
        try:
            return self.report(**sections)
        except Exception as exc:
            log.exception("status report failed")
            stamp = dt.datetime.now(dt.UTC).astimezone().strftime("%H:%M:%S")
            return f"⚠️ status unavailable ({stamp})\n`{type(exc).__name__}: {exc}`"

    def _plans(self) -> dict[str, ExitPlan]:
        """Read-only view of the supervisor's plans. Never writes the file."""
        try:
            raw = json.loads(self.plans_path.read_text(encoding="utf-8"))
            return {k: ExitPlan(**v) for k, v in raw.get("plans", {}).items()}
        except (OSError, ValueError, TypeError):
            return {}

    def report(
        self,
        sections: tuple[str, ...] = ("cash", "positions", "pnl", "orders", "api", "learning"),
    ) -> str:
        snapshot = self.state.reconcile()
        taken = snapshot.taken_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        net = " testnet" if self.cfg.broker.binance.use_testnet else ""
        lines = [f"*BINANCE{net}* — {taken}"]

        rows = {r.get("stk_cd"): r for r in snapshot.positions.get("rows", [])}
        plans = self._plans()

        if "cash" in sections:
            free = _flt(snapshot.cash.get("ord_alow_amt"))
            locked = _flt(snapshot.cash.get("locked"))
            equity = _flt(snapshot.positions.get("prsm_dpst_aset_amt"))
            lines += [
                "\n*Cash (USDT)*",
                f"  free {free:,.2f} · locked {locked:,.2f}",
                f"  account equity ≈ {equity:,.2f} (every balance marked to market)",
            ]

        if "positions" in sections:
            now = dt.datetime.now(dt.UTC)
            lines.append(f"\n*Managed positions* ({len(plans)})")
            if not plans:
                lines.append("  none — this agent holds nothing it bought")
            for symbol, plan in sorted(plans.items()):
                row = rows.get(symbol, {})
                qty = _flt(row.get("rmnd_qty")) or plan.quantity
                mark = _flt(row.get("cur_prc"))
                entry = plan.entry_price
                pl_pct = (mark / entry - 1) * 100 if entry and mark else 0.0
                lines.append(f"  *{symbol}*  x{qty:g} ≈ {qty * mark:,.2f} USDT")
                lines.append(f"    entry {entry:,.6g} → mark {mark:,.6g}  ({pl_pct:+.2f}%)")
                lines.append(
                    f"    stop {plan.stop:,.6g} · breakeven {plan.net_breakeven:,.6g}"
                    f" · target {plan.target:,.6g}"
                )
                held = f"    held {_hours_since(plan.opened_at, now):.1f}h"
                held += f" · high water {plan.high_water:,.6g}"
                if not row:
                    held += " · ⚠️ broker no longer reports this position"
                lines.append(held)

            unmanaged = [r for s, r in rows.items() if s not in plans]
            if unmanaged:
                value = sum(_flt(r.get("evlt_amt")) for r in unmanaged)
                lines.append(
                    f"  _unmanaged: {len(unmanaged)} balance(s) ≈ {value:,.2f} USDT"
                    " — no cost basis (seeds/deposits), never traded by this agent_"
                )

        if "pnl" in sections:
            # Both figures come from the LEDGER's FIFO walk, not from broker
            # balances: balances include seed units this system never paid for,
            # and marking those from a plan's entry would invent profit out of
            # free inventory. Ledger prices are mainnet reference prices, so
            # this is marked to the real market, not to testnet fills.
            since = self.cfg.score.trade_since
            markets = set(self.cfg.score.trade_markets)
            open_lots = self.ledger.open_lots(since=since, markets=markets)
            unrealised = 0.0
            marked = 0
            for symbol, lots_ in open_lots.items():
                mark = _flt((rows.get(symbol) or {}).get("cur_prc"))
                if not mark:
                    continue
                unrealised += sum((mark - price) * qty for qty, price in lots_)
                marked += 1
            closed = self.ledger.closed_trades(since=since, markets=markets)
            realised = sum(t["pnl_quote"] for t in closed)
            lines += [
                "\n*P&L (this epoch, marked to mainnet)*",
                f"  unrealised: {unrealised:+,.2f} USDT ({marked} open position(s), bought units only)",
                f"  realised  : {realised:+,.2f} USDT ({len(closed)} closed round trip(s))",
                f"  net       : {unrealised + realised:+,.2f} USDT before fees — /costs has the full arithmetic",
            ]

        if "orders" in sections:
            orders = snapshot.open_orders.get("rows", [])
            lines.append(f"\n*Open orders* ({len(orders)})")
            for o in orders[:12]:
                lines.append(
                    f"  {o.get('side', '?')} {o.get('symbol', '?')}"
                    f" x{_flt(o.get('origQty')):g} @ {_flt(o.get('price')):,.6g}"
                    f" — filled {_flt(o.get('executedQty')):g}"
                )
            if not orders:
                lines.append("  none")

        if "api" in sections:
            # The decide loop stops when the daily budget is spent (exits do
            # not), so the burn rate belongs in the operator's default view.
            day = self.ledger.day()
            budget = self.cfg.accounting.max_api_krw_per_day
            tokens = day.input_tokens + day.output_tokens
            lines.append("\n*DeepSeek spend (today)*")
            used = f"{day.api_krw:,.1f} KRW · {day.llm_calls} calls · {tokens:,} tokens"
            if budget:
                used += f" — {day.api_krw / budget:.0%} of {budget:,.0f} KRW budget"
            lines.append(f"  {used}")
            for model, m in sorted(self.ledger.llm_by_model().items()):
                lines.append(
                    f"  {model}: {m['calls']} calls · {m['tokens']:,} tokens · {m['krw']:,.1f} KRW"
                )
            if budget and day.api_krw >= budget:
                lines.append(
                    "  ⚠️ budget spent — deciding is paused until tomorrow; exits still run"
                )

        if "learning" in sections:
            # Progress of the experience corpus -- the phone is the only place
            # the owner can watch it fill.
            lines.append("\n*Learning (experience corpus)*")
            opens = resolves = 0
            obs_path = Path(self.cfg.score.observations)
            if obs_path.exists():
                for row in obs_path.read_text(encoding="utf-8").splitlines():
                    if '"kind": "open"' in row:
                        opens += 1
                    elif '"kind": "resolve"' in row:
                        resolves += 1
            lines.append(f"  observations: {opens} opened · {resolves} resolved")
            exp_path = Path(self.cfg.score.experience)
            if exp_path.exists():
                try:
                    exp = json.loads(exp_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    exp = {}
                min_n = self.cfg.score.min_bucket_n
                qualifying = [b for b in exp.get("buckets", []) if b.get("n", 0) >= min_n]
                lines.append(
                    f"  buckets in the prompt: {len(qualifying)} of {len(exp.get('buckets', []))}"
                    f" (gate n≥{min_n})"
                )
                pairs = exp.get("model_vs_shadow", {})
                if pairs.get("n"):
                    lines.append(
                        f"  model vs random: n={pairs['n']}"
                        f" · model {pairs.get('model_avg_pct', 0):+.2f}%"
                        f" vs random {pairs.get('shadow_avg_pct', 0):+.2f}%"
                    )
            else:
                lines.append("  no experience store yet — the scorer fills it hourly")

        return "\n".join(lines)
