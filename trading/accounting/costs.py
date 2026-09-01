"""Cost accounting and the break-even point.

A trade is not profitable because the price moved the right way. It is profitable
only after **trading costs** and after the **API spend that produced the decision**.
Both are recorded here, in one append-only ledger, so break-even is a measured
number rather than an assumption.

Trading costs in KR equities are asymmetric and mostly unavoidable:

    buy  : commission
    sell : commission + 거래세/농특세 on proceeds
    both : spread/slippage

At the configured rates a round trip costs roughly **0.28% of notional**, so a
strategy turning over the account once a day must gross ~0.28%/day just to stand
still. API spend then adds a fixed daily floor on top, which matters most when the
account is small: the same ₩500/day of tokens is trivial against ₩100M and fatal
against ₩5M.

:meth:`CostLedger.breakeven` reports exactly that: what the day's gross P&L must
exceed before any of it is yours.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from trading.config import AppConfig, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Usage:
    """One model call's token spend."""

    model: str
    provider: str
    tier: str
    input_tokens: int
    output_tokens: int
    usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class DayCosts:
    date: str
    api_usd: float = 0.0
    api_krw: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trade_fees_krw: float = 0.0
    traded_notional_krw: float = 0.0
    trades: int = 0
    realised_pl_krw: float = 0.0

    @property
    def total_cost_krw(self) -> float:
        return self.api_krw + self.trade_fees_krw

    @property
    def net_krw(self) -> float:
        """What actually reaches the account after every cost."""
        return self.realised_pl_krw - self.total_cost_krw

    @property
    def breakeven_gap_krw(self) -> float:
        """Gross P&L still needed today to break even. Zero once ahead."""
        return max(0.0, self.total_cost_krw - self.realised_pl_krw)


class CostLedger:
    """Append-only record of what the system spends, and what it made back."""

    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or config()
        self.acc = self.cfg.accounting
        self.path = Path(self.acc.ledger)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- pricing --------------------------------------------------------------

    def price_call(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """USD-equivalent for one call. Unknown models cost 0 and warn.

        A model priced in CNY (DeepSeek bills this account in CNY, from a CNY
        list that is not market-FX of the USD list) is converted at `usd_cny`,
        so the ledger's usd and krw figures both track the actual bill.
        """
        price = self.cfg.llm.pricing.get(model)
        if price is None:
            log.warning("no pricing configured for model %r; cost recorded as 0", model)
            return 0.0
        native = (input_tokens * price.input + output_tokens * price.output) / 1_000_000
        if price.currency == "CNY":
            return native / self.cfg.llm.usd_cny
        return native

    def trade_fee(self, notional: float, *, side: str, market: str | None = None) -> float:
        """Cost of one execution. Sells additionally pay any transaction tax."""
        fees = self.acc.fees_for(market)
        cost = notional * fees.commission_rate
        cost += notional * (fees.slippage_bps / 10_000)
        if str(side).upper() == "SELL":
            cost += notional * fees.sell_tax_rate
        return cost

    def round_trip_cost(self, notional: float, market: str | None = None) -> float:
        return notional * self.acc.fees_for(market).round_trip_rate()

    def to_krw(self, amount: float, currency: str) -> float:
        """Report-currency conversion. USDT amounts are treated as USD."""
        if str(currency).upper() in ("USD", "USDT"):
            return amount * self.cfg.llm.usd_krw
        if str(currency).upper() != "KRW":
            log.warning("unknown ledger currency %r; recorded unconverted", currency)
        return amount

    def breakeven_move_pct(self, market: str | None = None) -> float:
        """How far price must move on a round trip before costs are covered."""
        return self.acc.fees_for(market).round_trip_rate()

    # -- recording ------------------------------------------------------------

    def _append(self, kind: str, **fields) -> None:
        record = {"ts": dt.datetime.now(dt.UTC).isoformat(), "kind": kind, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.error("ledger write failed: %s", exc)

    def record_llm(self, usage: Usage) -> None:
        self._append(
            "llm",
            model=usage.model,
            provider=usage.provider,
            tier=usage.tier,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd=round(usage.usd, 6),
            krw=round(usage.usd * self.cfg.llm.usd_krw, 2),
        )

    def record_trade(
        self, *, symbol: str, side: str, quantity: float, price: float, market: str | None = None
    ) -> float:
        """Record one fill. The fee is computed and stored in the market's own
        quote currency (`fee`, `currency`) and converted once for the KRW report
        (`fee_krw`) — fees used to land in `fee_krw` unconverted, which
        under-reported Binance costs by the full FX rate. Returns the fee in
        quote units, which is what exit hurdles are computed in."""
        notional = abs(price * quantity)
        fee = self.trade_fee(notional, side=side, market=market)
        currency = self.acc.fees_for(market).currency
        self._append(
            "trade",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            market=market,
            notional=round(notional, 2),
            fee=round(fee, 4),
            currency=currency,
            fee_krw=round(self.to_krw(fee, currency), 2),
        )
        return fee

    def record_realised(self, amount: float, source: str = "broker", currency: str = "KRW") -> None:
        """Realised P&L as reported by the broker — the only authority on it.

        `amount` is in the broker's own currency; it is converted once here so
        the day report stays in KRW."""
        self._append(
            "realised",
            amount=round(amount, 4),
            currency=currency,
            krw=round(self.to_krw(amount, currency), 2),
            source=source,
        )

    def record_cash_flow(self, amount_krw: float, kind: str = "deposit") -> None:
        """An owner deposit or withdrawal — NOT performance.

        The account balance moves for two unrelated reasons: trading, and the
        owner adding or removing money. Any return computed as
        `(equity_now - equity_then) / equity_then` conflates them, so a deposit
        reads as a spectacular gain and a withdrawal as a catastrophic loss.

        Recording flows explicitly is what lets performance be measured as
        realised P&L net of costs (which is flow-invariant), or as a
        time-weighted return that neutralises them. Never subtract equities.
        """
        self._append("cash_flow", krw=round(amount_krw, 2), flow=kind)

    def cash_flows(self, day: dt.date | None = None) -> float:
        """Net owner-initiated flow for the day. Excluded from any P&L figure."""
        return sum(
            float(r.get("krw", 0))
            for r in self._records(day or dt.datetime.now(dt.UTC).date())
            if r.get("kind") == "cash_flow"
        )

    # -- reading --------------------------------------------------------------

    def _records(self, day: dt.date) -> list[dict]:
        if not self.path.exists():
            return []
        prefix = day.isoformat()
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("ts", "")).startswith(prefix):
                    out.append(rec)
        return out

    def day(self, day: dt.date | None = None) -> DayCosts:
        day = day or dt.datetime.now(dt.UTC).date()
        costs = DayCosts(date=day.isoformat())
        for rec in self._records(day):
            match rec.get("kind"):
                case "llm":
                    costs.llm_calls += 1
                    costs.api_usd += float(rec.get("usd", 0))
                    costs.api_krw += float(rec.get("krw", 0))
                    costs.input_tokens += int(rec.get("input_tokens", 0))
                    costs.output_tokens += int(rec.get("output_tokens", 0))
                case "trade":
                    costs.trades += 1
                    fee_krw = float(rec.get("fee_krw", 0))
                    if "currency" not in rec:
                        # Legacy row: fee_krw was written in the market's quote
                        # units without conversion. Convert on read — but only
                        # for markets with an explicit fee entry: rows from
                        # removed markets (KR/US) were genuinely KRW and must
                        # not be converted through the USD default block.
                        entry = self.acc.market_fees.get(str(rec.get("market")))
                        if entry is not None:
                            fee_krw = self.to_krw(fee_krw, entry.currency)
                    costs.trade_fees_krw += fee_krw
                    costs.traded_notional_krw += float(rec.get("notional", 0))
                case "realised":
                    # Last writer wins: realised P&L is a running total from the
                    # broker, not an increment to accumulate. (Legacy Binance
                    # rows stored USDT under `krw`; they age out with the day.)
                    costs.realised_pl_krw = float(rec.get("krw", 0))
        return costs

    def _walk_trades(
        self, since: str, markets: set[str] | None
    ) -> tuple[list[dict], dict[str, list[list[float]]]]:
        """FIFO walk over this ledger's own trade records.

        Returns (closed round trips, still-open lots). Prices in the ledger are
        DATA-PLANE reference prices — mainnet by construction since the plane
        split — so both results are marked to the real market rather than to
        testnet fills. Orphan sells (units no recorded buy paid for, e.g. seed
        liquidations) pair with nothing and appear in neither.
        """
        closed: list[dict] = []
        lots: dict[str, list[list[float]]] = {}  # symbol -> [[qty, price], ...]
        if not self.path.exists():
            return closed, lots
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "trade":
                    continue
                if since and str(rec.get("ts", "")) < since:
                    continue
                market = str(rec.get("market", ""))
                if markets and market not in markets:
                    continue
                symbol = rec.get("symbol", "")
                qty = float(rec.get("quantity") or 0)
                price = float(rec.get("price") or 0)
                if not symbol or qty <= 0 or price <= 0:
                    continue
                if str(rec.get("side", "")).upper().endswith("BUY"):
                    lots.setdefault(symbol, []).append([qty, price])
                    continue
                remaining = qty
                while remaining > 0 and lots.get(symbol):
                    lot = lots[symbol][0]
                    take = min(lot[0], remaining)
                    closed.append(
                        {
                            "symbol": symbol,
                            "market": market,
                            "quantity": take,
                            "entry_price": lot[1],
                            "exit_price": price,
                            "pnl_quote": take * (price - lot[1]),
                            "return_pct": (price / lot[1] - 1) * 100,
                        }
                    )
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 0:
                        lots[symbol].pop(0)
        return closed, {s: ls for s, ls in lots.items() if ls}

    def closed_trades(self, *, since: str = "", markets: set[str] | None = None) -> list[dict]:
        """Closed FIFO round trips, mark-to-mainnet. See `_walk_trades`."""
        return self._walk_trades(since, markets)[0]

    def open_lots(self, *, since: str = "", markets: set[str] | None = None) -> dict:
        """Bought-but-unsold lots — the units the ledger says are still ours.

        This is what honest unrealised P&L is computed over: broker balances
        include units this system never paid for (testnet seeds), and marking
        those from a plan's entry would invent profit out of free inventory.
        """
        return self._walk_trades(since, markets)[1]

    def llm_by_model(self, day: dt.date | None = None) -> dict[str, dict]:
        """Per-model API spend for the day — the operator's view of token cost."""
        day = day or dt.datetime.now(dt.UTC).date()
        out: dict[str, dict] = {}
        for rec in self._records(day):
            if rec.get("kind") != "llm":
                continue
            m = out.setdefault(
                str(rec.get("model", "?")), {"calls": 0, "tokens": 0, "usd": 0.0, "krw": 0.0}
            )
            m["calls"] += 1
            m["tokens"] += int(rec.get("input_tokens", 0)) + int(rec.get("output_tokens", 0))
            m["usd"] += float(rec.get("usd", 0))
            m["krw"] += float(rec.get("krw", 0))
        return out

    def breakeven(self, day: dt.date | None = None, market: str | None = None) -> str:
        """Human-readable break-even status for the day."""
        c = self.day(day)
        tokens = c.input_tokens + c.output_tokens
        api_detail = f"(${c.api_usd:.4f}, {c.llm_calls} calls, {tokens:,} tokens)"
        fee_detail = f"({c.trades} fills, {c.traded_notional_krw:,.0f} quote notional)"
        lines = [
            f"*Break-even — {c.date}*",
            f"  API spend    : {c.api_krw:>12,.0f} KRW  {api_detail}",
        ]
        # Per model, because the whole point of the tier config is that the mix
        # is a cost decision: one reasoner call costs ~5x a chat call.
        for model, m in sorted(self.llm_by_model(day).items()):
            lines.append(
                f"    {model}: {m['calls']} calls, {m['tokens']:,} tokens, {m['krw']:,.1f} KRW"
            )
        budget = self.acc.max_api_krw_per_day
        if budget:
            lines.append(
                f"  API budget   : {c.api_krw:,.1f} / {budget:,.0f} KRW"
                f" ({c.api_krw / budget:.0%} used — deciding stops when spent)"
            )
        lines += [
            f"  Trading fees : {c.trade_fees_krw:>12,.0f} KRW  {fee_detail}",
            f"  Total cost   : {c.total_cost_krw:>12,.0f} KRW",
            # Flat symbols only, by construction upstream: an open position's
            # buys are committed cash, not a realised loss.
            f"  Realised P/L : {c.realised_pl_krw:>12,.0f} KRW  (closed symbols only)",
            f"  *Net*        : {c.net_krw:>12,.0f} KRW",
        ]
        if c.breakeven_gap_krw > 0:
            lines.append(f"  ⚠️ needs {c.breakeven_gap_krw:,.0f} KRW more to break even")
        else:
            lines.append("  ✅ past break-even")
        note = (
            f"  round trip costs {self.breakeven_move_pct(market):.3%} of notional — "
            "a trade must clear that before it earns anything"
        )
        lines.append(note)
        return "\n".join(lines)
