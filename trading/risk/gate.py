"""The risk gate: the last thing before an order, and never a model.

An LLM proposes a :class:`TradeIntent`; this module decides whether it may be
sent. Every check is deterministic arithmetic over broker-reported state, so no
prompt, jailbreak or hallucinated field can widen a limit. The gate is fail-closed
throughout: anything it cannot verify -- unreadable state, unpriceable order,
missing evaluation total -- is a rejection, not a pass.

Order of checks matters. The kill switch is first because it must work even when
every other subsystem is broken, and it lives in a file so it survives this
process dying.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from trading.brokers.state import Snapshot
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class TradeIntent:
    """What the model wants to do. Untrusted until the gate approves it."""

    market: str
    side: Side
    symbol: str
    quantity: int
    # None means market order. The gate still needs a price to value the order,
    # so `reference_price` must be supplied for market orders.
    limit_price: float | None = None
    reference_price: float | None = None
    reason: str = ""
    confidence: float = 0.0

    @property
    def price_for_valuation(self) -> float | None:
        return self.limit_price if self.limit_price is not None else self.reference_price

    @property
    def value(self) -> float | None:
        price = self.price_for_valuation
        return None if price is None else price * self.quantity


@dataclass(slots=True)
class Verdict:
    approved: bool
    intent: TradeIntent
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        mark = "APPROVED" if self.approved else "REJECTED"
        detail = "; ".join(self.reasons) if self.reasons else "ok"
        return f"{mark} {self.intent.side} {self.intent.quantity} {self.intent.symbol} — {detail}"


def _f(value) -> float:
    """Broker numerics may arrive as strings; unparseable -> 0.0."""
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


def _rows(payload: dict) -> list[dict]:
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


class RiskGate:
    def __init__(
        self,
        state,
        cfg: AppConfig | None = None,
        market: str | None = None,
        pnl_provider=None,
    ):
        self.cfg = cfg or config()
        self.risk = self.cfg.risk
        self.state = state
        # The kill switch is PER VENUE: `data/HALT` halts everything (the
        # emergency stop); `data/HALT.BINANCE` halts only that venue.
        # The gate validates against the market it was BUILT for, not the global
        # config default -- comparing an intent to `agent.market` once rejected
        # every order from a gate built for a different venue.
        self.market = str(market) if market else str(self.cfg.agent.market)
        base = Path(self.risk.kill_switch_file)
        self.global_halt_file = base
        self.halt_file = base.with_name(f"{base.name}.{market}") if market else base
        # Realised P&L must come from the venue's own reporting, injected by the
        # adapter -- the gate itself knows no broker API.
        self._pnl_provider = pnl_provider
        self._orders_today = 0
        self._orders_day: dt.date | None = None

    # -- kill switch ----------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self.halt_file.exists() or self.global_halt_file.exists()

    def halt(self, reason: str) -> None:
        self.halt_file.parent.mkdir(parents=True, exist_ok=True)
        self.halt_file.write_text(
            f"{dt.datetime.now(dt.UTC).isoformat()} {reason}\n", encoding="utf-8"
        )
        log.critical("KILL SWITCH SET: %s", reason)

    def clear_halt(self) -> None:
        """Clear this venue's halt. The global stop must be cleared deliberately."""
        if self.halt_file.exists():
            self.halt_file.unlink()

    # -- daily counters -------------------------------------------------------

    def _roll_day(self) -> None:
        today = dt.datetime.now(dt.UTC).date()
        if self._orders_day != today:
            self._orders_day, self._orders_today = today, 0

    def record_sent(self) -> None:
        """Call after an order is actually transmitted."""
        self._roll_day()
        self._orders_today += 1

    # -- individual checks ----------------------------------------------------

    def _check_shape(self, intent: TradeIntent, reasons: list[str]) -> None:
        if intent.quantity <= 0:
            reasons.append(f"quantity must be positive, got {intent.quantity}")
        if not intent.symbol:
            reasons.append("missing symbol")
        if intent.market != self.market:
            reasons.append(f"intent market {intent.market} != this gate's market {self.market}")

    def _check_value(self, intent: TradeIntent, reasons: list[str]) -> None:
        # The per-order cap is a SIZING control, so it applies to entries only.
        # Applying it to a sell would make a position larger than the cap
        # impossible to close — the cap would trap you in the very position it
        # was meant to keep small.
        if intent.side is Side.SELL:
            return
        value = intent.value
        if value is None:
            # Fail closed: an order we cannot price is an order we cannot bound.
            reasons.append("no limit_price or reference_price, so order value is unknown")
            return
        if self.risk.max_order_value_krw and value > self.risk.max_order_value_krw:
            reasons.append(
                f"order value {value:,.0f} exceeds max_order_value_krw "
                f"{self.risk.max_order_value_krw:,.0f}"
            )

    def _equity(self, snap: Snapshot) -> float:
        """Total account equity — the denominator for concentration.

        `tot_evlt_amt` is the value of *holdings only*, so using it would measure a
        position against the rest of the portfolio rather than against the account:
        in a mostly-cash account it makes the cap absurdly tight, and in an empty
        one it divides by zero. 추정예탁자산 is the whole-account figure; fall back
        to cash + holdings when the broker omits it.
        """
        estimated = _f(snap.positions.get("prsm_dpst_aset_amt"))
        if estimated > 0:
            return estimated
        return _f(snap.cash.get("entr")) + _f(snap.positions.get("tot_evlt_amt"))

    def _check_position_cap(self, intent: TradeIntent, snap: Snapshot, reasons: list[str]) -> None:
        if intent.side is Side.SELL or not self.risk.max_position_pct:
            return
        total = self._equity(snap)
        if total <= 0:
            reasons.append("account equity unavailable, cannot enforce max_position_pct")
            return
        held = 0.0
        for row in _rows(snap.positions):
            if row.get("stk_cd", "").lstrip("A") == intent.symbol.lstrip("A"):
                held = _f(row.get("evlt_amt") or row.get("cur_prc_evlt_amt"))
                break
        value = intent.value or 0.0
        share = (held + value) / total
        if share > self.risk.max_position_pct:
            reasons.append(
                f"post-trade position {share:.1%} of account exceeds max_position_pct "
                f"{self.risk.max_position_pct:.1%}"
            )

    def _check_cash(self, intent: TradeIntent, snap: Snapshot, reasons: list[str]) -> None:
        if intent.side is Side.SELL:
            return
        available = _f(snap.cash.get("entr") or snap.cash.get("ord_alow_amt"))
        value = intent.value
        if value is not None and available and value > available:
            reasons.append(f"order value {value:,.0f} exceeds available cash {available:,.0f}")

    def _check_holding_for_sell(
        self, intent: TradeIntent, snap: Snapshot, reasons: list[str]
    ) -> None:
        if intent.side is not Side.SELL:
            return
        for row in _rows(snap.positions):
            if row.get("stk_cd", "").lstrip("A") == intent.symbol.lstrip("A"):
                qty = _f(row.get("rmnd_qty") or row.get("hldg_qty"))
                if intent.quantity > qty:
                    reasons.append(f"sell {intent.quantity} exceeds holding {qty:,.0f}")
                return
        reasons.append(f"no holding of {intent.symbol} to sell")

    def _daily_loss_cap(self, snap: Snapshot) -> float:
        """Absolute loss cap for this venue, in the venue's own currency."""
        if self.risk.max_daily_loss_pct:
            return self._equity(snap) * self.risk.max_daily_loss_pct
        return self.risk.max_daily_loss_krw

    def _check_daily_loss(
        self, intent: TradeIntent, reasons: list[str], snap: Snapshot | None = None
    ) -> None:
        # A breached loss cap must stop NEW risk, never trap an open position.
        # Applying it to sells contradicts every other exemption here and would
        # strand exactly the position that caused the breach.
        if intent.side is Side.SELL:
            return
        cap = self._daily_loss_cap(snap) if snap is not None else self.risk.max_daily_loss_krw
        if not cap:
            return
        if self._pnl_provider is None:
            return
        try:
            realised = self._pnl_provider()
        except Exception as exc:  # noqa: BLE001 - fail closed on an unreadable limit
            reasons.append(f"daily P/L unreadable ({type(exc).__name__}), refusing to trade blind")
            return
        if realised is not None and realised < 0 and abs(realised) >= cap:
            reasons.append(
                f"daily realised loss {abs(realised):,.2f} has reached the cap {cap:,.2f}"
            )

    def _check_rate(self, intent: TradeIntent, reasons: list[str]) -> None:
        # Churn limits bound how much NEW risk is taken. Refusing an exit because
        # the day's entry budget is spent would strand an open position.
        if intent.side is Side.SELL:
            return
        self._roll_day()
        if self.risk.max_orders_per_day and self._orders_today >= self.risk.max_orders_per_day:
            reasons.append(
                f"already sent {self._orders_today} orders today, limit is "
                f"{self.risk.max_orders_per_day}"
            )

    # -- entry point ----------------------------------------------------------

    def evaluate(self, intent: TradeIntent) -> Verdict:
        """Approve or reject one intent against freshly reconciled broker state."""
        reasons: list[str] = []

        # A halt stops NEW risk. Blocking exits too would lock the account into
        # whatever it held when the switch fired, turning a safety control into a
        # trap, so a reducing order is still allowed through.
        reduces_risk = intent.side is Side.SELL
        if self.halted and not (reduces_risk and self.cfg.exits.exits_allowed_under_halt):
            return Verdict(False, intent, [f"kill switch is set ({self.halt_file})"])
        if not self.risk.enabled:
            # Disabling the gate is not a supported way to trade.
            return Verdict(False, intent, ["risk.enabled is false; refusing to place orders"])

        self._check_shape(intent, reasons)
        self._check_value(intent, reasons)
        self._check_rate(intent, reasons)

        try:
            # SST: never judge an order against a cached view of the account.
            snap = self.state.assert_reconciled()
        except Exception as exc:  # noqa: BLE001 - fail closed
            reasons.append(f"broker state unavailable ({type(exc).__name__}: {exc})")
            return Verdict(False, intent, reasons)

        self._check_cash(intent, snap, reasons)
        self._check_position_cap(intent, snap, reasons)
        self._check_holding_for_sell(intent, snap, reasons)
        self._check_daily_loss(intent, reasons, snap)

        verdict = Verdict(not reasons, intent, reasons)
        log.info("risk gate: %s", verdict)
        return verdict

    def evaluate_all(self, intents: list[TradeIntent]) -> list[Verdict]:
        """Evaluate a cycle's intents, enforcing the per-cycle cap."""
        verdicts = [self.evaluate(i) for i in intents]
        cap = self.risk.max_orders_per_cycle
        if not cap:  # 0 = unlimited
            return verdicts
        # Only entries consume the per-cycle budget; exits are never rationed.
        approved = [v for v in verdicts if v.approved and v.intent.side is not Side.SELL]
        for extra in approved[cap:]:
            extra.approved = False
            extra.reasons.append(f"exceeds max_orders_per_cycle {cap}")
        return verdicts
