"""Cost-derived exits, enforced without the model.

The premise of this system is that a trade is only profitable after trading fees
**and** the API spend that produced it. Exits follow from that directly rather
than from R-multiples off an arbitrary stop:

* A position's real break-even is not its entry price. It is entry marked up by
  the round-trip cost rate, plus that position's share of the day's API spend.
  Selling at "entry + a bit" is a loss, and this module refuses to call it a win.
* Every level is a multiple of that hurdle, so when fees or model prices change,
  the targets re-derive themselves instead of going quietly stale.
* Time is a cost. A position that has not cleared its hurdle within
  ``max_hold_minutes`` is paying to do nothing and is closed.

Two structural rules, both consequences of this system's own design:

**The supervisor runs on its own clock.** The decision loop is bounded by a daily
API budget and will stop mid-session by design. An exit that needs the model to
be alive and affordable is not an exit, so nothing here calls an LLM.

**A halt blocks new risk, never risk reduction.** A kill switch that also prevents
selling would lock the account into whatever it was holding when it fired. Exits
are explicitly permitted while halted.

Position quantities and prices always come from the broker. What is persisted
here is *policy* — the stop level this system committed to — reconciled against
broker holdings on every pass, and dropped when the broker says the position is
gone.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.config import AppConfig, config

log = logging.getLogger(__name__)


class ExitReason(StrEnum):
    STOP = "stop_loss"
    TARGET = "target"
    TRAIL = "trailing_stop"
    TIME = "max_hold"
    UNIVERSE = "left_universe"


@dataclass(slots=True)
class ExitPlan:
    """The committed exit levels for one holding.

    `stop` is a floor: it may ratchet up as a trail earns it, never down. Widening
    a stop after entry is how a small loss becomes a large one, so it is refused
    in code rather than left to judgement.
    """

    symbol: str
    entry_price: float
    quantity: float
    opened_at: str
    stop: float
    net_breakeven: float
    target: float
    high_water: float = 0.0
    api_cost_share: float = 0.0

    def tighten_stop(self, candidate: float) -> bool:
        """Raise the stop. Returns True if it moved; never lowers it."""
        if candidate > self.stop:
            self.stop = candidate
            return True
        return False


@dataclass(slots=True)
class ExitSignal:
    symbol: str
    quantity: float
    reason: ExitReason
    price: float
    detail: str = ""

    def __str__(self) -> str:
        return f"EXIT {self.symbol} x{self.quantity:g} [{self.reason}] @ {self.price:,.6g} — {self.detail}"


@dataclass(slots=True)
class ExitState:
    plans: dict[str, ExitPlan] = field(default_factory=dict)


class ExitPolicy:
    """Turns the cost model into concrete price levels."""

    def __init__(
        self,
        cfg: AppConfig | None = None,
        ledger: CostLedger | None = None,
        market: str | None = None,
    ):
        self.cfg = cfg or config()
        self.market = market
        self.ecfg = self.cfg.exits.for_market(market)
        self.ledger = ledger or CostLedger(self.cfg)

    @property
    def hurdle(self) -> float:
        """Round-trip cost as a fraction of notional — the minimum honest gain.

        MUST pass the market. Omitting it fell back to the KR default and priced
        every Binance exit at 0.280% instead of 0.600%, setting targets less than
        half as far away as the venue actually costs.
        """
        return self.ledger.breakeven_move_pct(self.market)

    def net_breakeven(self, entry_price: float, quantity: float, api_share: float = 0.0) -> float:
        """The price at which this position is genuinely flat, not nominally flat."""
        gross = entry_price * (1 + self.hurdle)
        if quantity > 0 and api_share > 0:
            gross += api_share / quantity
        return gross

    def plan_for(
        self,
        symbol: str,
        entry_price: float,
        quantity: float,
        *,
        api_share: float = 0.0,
        opened_at: str | None = None,
    ) -> ExitPlan:
        breakeven = self.net_breakeven(entry_price, quantity, api_share)
        stop = entry_price * (1 - self.ecfg.stop_loss_pct)

        # Two candidate targets, and the wider one wins.
        #
        # The hurdle-derived target alone is unsafe: with a 3% stop and a 0.28%
        # hurdle, a 4x-hurdle target is +1.1% against -3% risk — a 0.34 reward:risk
        # that needs a ~75% hit rate merely to break even. Risk measured from NET
        # breakeven (not entry) is the real downside, so the floor is expressed
        # against that, and `min_reward_risk` becomes a guarantee rather than a hope.
        risk = max(breakeven - stop, 0.0)
        by_hurdle = breakeven * (1 + self.ecfg.target_hurdle_multiple * self.hurdle)
        by_ratio = breakeven + self.ecfg.min_reward_risk * risk
        target = max(by_hurdle, by_ratio)

        return ExitPlan(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            opened_at=opened_at or dt.datetime.now(dt.UTC).isoformat(),
            stop=stop,
            net_breakeven=breakeven,
            target=target,
            high_water=entry_price,
            api_cost_share=api_share,
        )

    def reward_risk(self, plan: ExitPlan) -> float:
        """Reward:risk measured from NET breakeven, which is the honest origin."""
        risk = plan.net_breakeven - plan.stop
        return float("inf") if risk <= 0 else (plan.target - plan.net_breakeven) / risk

    def trail_stop_for(self, plan: ExitPlan, price: float) -> float | None:
        """Trailing stop, armed only once the position is genuinely ahead.

        Trailing from entry would ratchet a stop up on a position that has not yet
        covered its own costs, turning cost drag into a stop-out.
        """
        arm_at = plan.net_breakeven * (1 + self.ecfg.trail_arm_hurdle_multiple * self.hurdle)
        if price < arm_at:
            return None
        run = price - plan.net_breakeven
        return price - run * self.ecfg.trail_give_back

    def evaluate(
        self, plan: ExitPlan, price: float, now: dt.datetime | None = None
    ) -> ExitSignal | None:
        """Decide whether this holding should be closed at `price`."""
        now = now or dt.datetime.now(dt.UTC)

        # Stop first: a breach is not negotiable against any other consideration.
        if price <= plan.stop:
            return ExitSignal(
                plan.symbol,
                plan.quantity,
                ExitReason.STOP,
                price,
                f"price {price:,.6g} at or below stop {plan.stop:,.6g}",
            )

        if price >= plan.target:
            return ExitSignal(
                plan.symbol,
                plan.quantity,
                ExitReason.TARGET,
                price,
                f"target {plan.target:,.6g} reached (net breakeven {plan.net_breakeven:,.6g})",
            )

        try:
            opened = dt.datetime.fromisoformat(plan.opened_at)
        except ValueError:
            opened = now
        held_min = (now - opened).total_seconds() / 60
        if self.ecfg.max_hold_minutes and held_min >= self.ecfg.max_hold_minutes:
            return ExitSignal(
                plan.symbol,
                plan.quantity,
                ExitReason.TIME,
                price,
                f"held {held_min:.0f}m without clearing {plan.net_breakeven:,.6g}",
            )
        return None


def exit_state_path(cfg: AppConfig, market: str | None) -> Path:
    """Where one market's exit plans persist.

    Shared by the supervisor that writes the file and the status reporters that
    read it, so the two can never derive different paths.
    """
    base = Path(cfg.exits.for_market(market).state)
    return base.with_name(f"{base.stem}_{market or 'default'}{base.suffix}")


class PositionSupervisor:
    """Watches broker holdings and emits exit signals. Never calls a model."""

    def __init__(
        self,
        state_reader,
        cfg: AppConfig | None = None,
        policy: ExitPolicy | None = None,
        market: str | None = None,
        is_dust=None,
    ):
        self.cfg = cfg or config()
        self.market = market
        self.ecfg = self.cfg.exits.for_market(market)
        self.state = state_reader
        self.policy = policy or ExitPolicy(self.cfg, market=market)
        # PER MARKET. Both services wrote one shared file, which corrupted it --
        # so no plan ever persisted, every position was re-adopted from scratch,
        # and the stop was recomputed from the FALLING price each cycle. An 8%
        # stop that trails downward can never fire. TUTUSDT lost 13.7% this way.
        self.path = exit_state_path(self.cfg, market)
        self.plans: dict[str, ExitPlan] = {}
        # Holdings refused for want of a cost basis, so the refusal is logged
        # when the set CHANGES rather than on every pass -- the testnet seeds
        # ~480 such balances, which was 482 error lines per cycle.
        self._refused: set[str] = set()
        # Venue-aware dust test injected by the loop: (symbol, qty, price) ->
        # True when that holding cannot form a valid order. The supervisor
        # knows nothing about lot rules, but it must not keep a plan alive for
        # a position that can never be sold: a quantized exit left 0.34 PROM
        # (~$2, under the $5 minNotional) and its stop refired every cycle
        # forever, each order refused (observed live 2026-08-31).
        self.is_dust = is_dust
        self._dust: set[str] = set()
        self.load()

    # -- persistence ----------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.plans = {k: ExitPlan(**v) for k, v in raw.get("plans", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            log.error("exit state unreadable (%s); starting empty", exc)
            self.plans = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(
                json.dumps({"plans": {k: asdict(v) for k, v in self.plans.items()}}, indent=1),
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("exit state write failed: %s", exc)

    # -- reconciliation -------------------------------------------------------

    def reconcile(self, holdings: dict[str, dict], prices: dict[str, float] | None = None) -> None:
        """Align plans with what the broker actually reports.

        Broker records are the source of truth for existence and quantity. A plan
        for a position the broker no longer reports is dropped; a holding with no
        plan is adopted with a fresh one, so a position opened by hand — or
        inherited on restart — is still protected. A holding that cannot form a
        valid order (dust) is never adopted: a plan that can only ever emit
        refused orders protects nothing and alarms forever.
        """
        prices = prices or {}
        for symbol in list(self.plans):
            if symbol not in holdings:
                log.info("exit plan for %s dropped: broker reports no position", symbol)
                del self.plans[symbol]

        refused: set[str] = set()
        dust: set[str] = set()
        for symbol, row in holdings.items():
            qty = float(row.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            plan = self.plans.get(symbol)
            if plan is None:
                price = float(prices.get(symbol) or 0)
                if self.is_dust and price > 0 and self.is_dust(symbol, qty, price):
                    dust.add(symbol)
                    continue
                # `cost_basis` is the REAL average paid, from the broker's fills.
                # `avg_price` may be the current price on venues that report only
                # balances -- adopting at that would recreate the trailing-stop bug.
                entry = float(row.get("cost_basis") or 0)
                if entry <= 0:
                    refused.add(symbol)
                    continue
                self.plans[symbol] = self.policy.plan_for(symbol, entry, qty)
                # %g, not %.0f: sub-cent crypto stops render as "0" otherwise.
                log.warning(
                    "adopted unmanaged position %s x%g; stop %g",
                    symbol,
                    qty,
                    self.plans[symbol].stop,
                )
            elif plan.quantity != qty:
                # Partial fill or manual trim: follow the broker, keep the stop.
                log.info("%s quantity %g -> %g (broker)", symbol, plan.quantity, qty)
                plan.quantity = qty

        if refused and refused != self._refused:
            shown = ", ".join(sorted(refused)[:8]) + ("…" if len(refused) > 8 else "")
            log.warning(
                "%d holding(s) with no cost basis left unmanaged (%s). Close them "
                "manually or supply the entry price -- a stop derived from the "
                "current price cannot protect anything.",
                len(refused),
                shown,
            )
        self._refused = refused
        if dust and dust != self._dust:
            log.info(
                "%d dust holding(s) below the venue minimum left unmanaged (%s)",
                len(dust),
                ", ".join(sorted(dust)[:8]),
            )
        self._dust = dust

    # -- the pass -------------------------------------------------------------

    def check(self, holdings: dict[str, dict], prices: dict[str, float]) -> list[ExitSignal]:
        """One supervision pass. Returns exits that should be sent now."""
        if not self.ecfg.enabled:
            return []
        self.reconcile(holdings, prices)

        signals: list[ExitSignal] = []
        for symbol, plan in list(self.plans.items()):
            price = prices.get(symbol)
            if price is None or price <= 0:
                # No price means no judgement. Do not guess a stop breach.
                log.warning("no price for %s; exit checks skipped this pass", symbol)
                continue

            if self.is_dust and self.is_dust(symbol, plan.quantity, price):
                # A quantized exit sold the bulk and left a remainder no valid
                # order can carry. The plan is done: keep it and its stop fires
                # every cycle into a refused order, forever.
                log.warning(
                    "%s x%g is dust (cannot form a valid order); plan closed, "
                    "remainder stays as an unmanaged balance",
                    symbol,
                    plan.quantity,
                )
                del self.plans[symbol]
                continue

            plan.high_water = max(plan.high_water, price)
            trail = self.policy.trail_stop_for(plan, price)
            if trail is not None and plan.tighten_stop(trail):
                log.info("%s trailing stop raised to %g", symbol, plan.stop)

            signal = self.policy.evaluate(plan, price)
            if signal:
                signals.append(signal)

        self.save()
        return signals
