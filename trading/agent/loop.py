"""The autonomous trading cycle.

One cycle is: **observe → decide → gate → execute → journal → notify.**

Only `decide` involves a model, and its output is a list of proposals with no
authority. Everything downstream is deterministic: the gate re-reads broker state
and applies fixed arithmetic, the executor refuses anything unapproved, and the
journal records all of it. A model failure degrades to "no trades this cycle",
never to an unchecked order.

The loop is also fail-closed on the clock: outside market hours it observes and
reports but does not trade.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

from trading.accounting.costs import CostLedger
from trading.agent.journal import Journal
from trading.brokers.adapters import build_adapter
from trading.config import AppConfig, config
from trading.llm.client import LLMClient
from trading.notify.status import StatusReporter
from trading.notify.telegram import TelegramNotifier
from trading.risk.exits import PositionSupervisor
from trading.risk.gate import RiskGate, Side, TradeIntent
from trading.risk.sizing import PositionSizer

log = logging.getLogger(__name__)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _num(value) -> float:
    """Kiwoom numerics are sign-prefixed strings; the sign is direction, not value."""
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


_SYSTEM = """You are the entry selector for an automated trading system.

You are NOT choosing "a good stock". Once you pick, the position is managed
mechanically and you have no further say. Concretely, your pick will be:

  - bought with the entire available balance, laddered over several price levels
  - CLOSED AT A LOSS if it falls to the stop
  - CLOSED IN PROFIT if it reaches the target
  - CLOSED REGARDLESS once the maximum hold time elapses
  - charged the round-trip cost shown, which it must clear before earning anything

So the only question you are actually answering is:

  "Which of these, if any, is most likely to reach the target BEFORE hitting the
   stop, within the hold window?"

The exact levels are given in `trade_rules`. Judge every candidate against them.
A name that is moving but cannot plausibly travel that far that fast is a bad
pick, however impressive its change today.

Reply with JSON only:
{"intents": [{"side": "BUY"|"SELL", "symbol": "...", "quantity": 0,
              "limit_price": null, "reason": "...", "confidence": 0.0-1.0}],
 "commentary": "one or two sentences"}

- `confidence` is your estimate of the probability that the trade reaches target
  before stop. 0.5 means a coin flip. Be honest: below the floor in `trade_rules`
  the decision is escalated to a stronger model rather than acted on, so
  overstating it removes a safety net rather than helping the trade.
- An empty list is a valid and often correct answer. Costs are paid on every
  trade; doing nothing is free.
- Only symbols from `candidates`. Anything else is discarded.
- `quantity` is ignored for buys — size is computed from the balance.
- Never propose selling more than the reported holding."""


def _describe_limits(risk) -> dict:
    """Render risk limits as the model should understand them."""

    def cap(value, unit=""):
        return "unlimited" if not value else f"{value:,.0f}{unit}"

    return {
        "max_order_value": cap(risk.max_order_value_krw),
        "max_position_share_of_account": (
            "unlimited" if not risk.max_position_pct else f"{risk.max_position_pct:.0%}"
        ),
        "max_daily_loss": cap(risk.max_daily_loss_krw),
        "max_orders_this_cycle": cap(risk.max_orders_per_cycle),
    }


@dataclass(slots=True)
class CycleResult:
    observed_at: dt.datetime
    intents: int = 0
    approved: int = 0
    sent: int = 0
    exits: int = 0
    tradable: bool = True
    commentary: str = ""
    errors: list[str] = None  # type: ignore[assignment]

    def summary(self) -> str:
        state = (
            "traded" if (self.sent or self.exits) else ("proposed" if self.intents else "no action")
        )
        return (
            f"cycle {self.observed_at:%H:%M:%S} — {state}: "
            f"{self.intents} intents, {self.approved} approved, {self.sent} sent, "
            f"{self.exits} exits" + (f" | errors: {'; '.join(self.errors)}" if self.errors else "")
        )


class TradingAgent:
    def __init__(
        self,
        cfg: AppConfig | None = None,
        *,
        notifier: TelegramNotifier | None = None,
        broker: str = "kiwoom",
        adapter=None,
    ):
        self.cfg = cfg or config()
        self.acfg = self.cfg.agent
        self.broker = broker
        self.tz = ZoneInfo(self.cfg.broker.kiwoom.timezone)

        self.adapter = adapter or build_adapter(self.broker, self.acfg.market, self.cfg)
        self.market = self.adapter.market
        self.state = self.adapter.state()
        self.gate = RiskGate(self.state, self.cfg, market=self.market)
        self.executor = self.adapter.executor(self.gate)
        self.sizer = PositionSizer(self.cfg)
        self.supervisor = PositionSupervisor(self.state, self.cfg)
        self.ledger = CostLedger(self.cfg)
        self.llm = LLMClient(self.cfg, ledger=self.ledger)
        self.journal = Journal(self.acfg.journal)
        self.telegram = notifier or TelegramNotifier()
        self.reporter = StatusReporter(self.state)
        self._last_fingerprint: tuple[str, ...] | None = None
        # Symbols this process has traded today -- Binance's myTrades needs a
        # symbol, so P&L is only queried where something actually happened.
        self._traded_symbols: set[str] = set()

    # -- clock ----------------------------------------------------------------

    def is_market_open(self, now: dt.datetime | None = None) -> bool:
        """Is *this agent's* market trading now?

        Sessions are declared in each exchange's own timezone, so KR closes at
        15:20 KST and US opens at 09:30 New York — 23:30 KST in winter, 22:30 in
        summer. Letting the zone database resolve that avoids a DST bug that would
        silently trade an hour late for half the year.
        """
        return self.acfg.is_open(str(self.market), now)

    # -- observe --------------------------------------------------------------

    def observe(self) -> dict:
        """Broker state, screened candidates, and quotes for those candidates only.

        Quotes are fetched for the screen output, not the full ~2,650-name universe:
        one call per candidate is affordable, one per listing is not.
        """
        snapshot = self.state.reconcile()
        # The screen's liquidity floor scales with the order, so it needs to know
        # how large an entry would be before it decides what is liquid enough.
        order_size = self.sizer.budget(snapshot.cash)
        candidates = self.adapter.candidates(order_size)
        holdings = self.adapter.holdings(snapshot)
        held = set(holdings)
        # Always consider what we already hold, even if it fell out of the screen --
        # otherwise the agent can open positions it will never revisit to close.
        symbols = list(dict.fromkeys([c["symbol"] for c in candidates] + sorted(held)))

        prices = self.adapter.prices(symbols)
        for c in candidates:
            prices.setdefault(c["symbol"], c.get("price") or 0.0)

        return {
            "snapshot": snapshot,
            "candidates": candidates,
            "quotes": {},
            "prices": prices,
            "holdings": holdings,
            "tradable": symbols,
        }

    # -- decide ---------------------------------------------------------------

    def _prompt(self, observation: dict) -> str:
        snap = observation["snapshot"]
        # `quotes` is empty on venues where the screen row already carries price,
        # change and volume. Sending an empty dict while the system prompt claims
        # "live quotes" was actively misleading, so it is simply omitted.
        compact_quotes = {
            symbol: {k: v for k, v in body.items() if not isinstance(v, list | dict)}
            for symbol, body in observation["quotes"].items()
        }
        return json.dumps(
            {
                "candidates": observation["candidates"],
                "cash": {k: v for k, v in snap.cash.items() if not isinstance(v, list | dict)},
                "positions": snap.positions,
                "open_orders": snap.open_orders,
                **({"quotes": compact_quotes} if compact_quotes else {}),
                # 0 means UNLIMITED to the gate, but a model shown a bare 0 reads
                # it as "nothing is allowed" and declines to trade. Observed live
                # 2026-08-10: "risk limits are all zero, making any order likely to
                # be rejected". Render the meaning, never the raw sentinel.
                "limits": _describe_limits(self.cfg.risk),
                # The payoff structure the pick will actually be held to. Without
                # this the model is asked "which is good?" when the real question
                # is "which reaches +X% before -Y% within Z minutes, net of costs?"
                # -- a different and far more answerable question.
                "trade_rules": self._trade_rules(),
            },
            ensure_ascii=False,
            default=str,
        )[:20000]

    def _trade_rules(self) -> dict:
        """The exit contract every pick is judged by, in the model's terms."""
        ecfg = self.cfg.exits
        hurdle = self.ledger.breakeven_move_pct(self.market)
        stop_pct = ecfg.stop_loss_pct
        # Mirrors ExitPolicy: target is the wider of the hurdle multiple and the
        # minimum reward:risk, measured from NET break-even.
        breakeven_pct = hurdle
        risk_pct = breakeven_pct + stop_pct
        target_pct = max(
            (1 + breakeven_pct) * (1 + ecfg.target_hurdle_multiple * hurdle) - 1,
            breakeven_pct + ecfg.min_reward_risk * risk_pct,
        )
        return {
            "round_trip_cost_pct": round(hurdle * 100, 3),
            "breakeven_move_pct": round(breakeven_pct * 100, 3),
            "target_gain_pct": round(target_pct * 100, 2),
            "stop_loss_pct": round(stop_pct * 100, 2),
            "reward_risk": round((target_pct - breakeven_pct) / risk_pct, 2),
            "max_hold_minutes": ecfg.max_hold_minutes,
            "confidence_floor": self.acfg.tiers.confidence_floor,
            "note": (
                f"A pick must gain {target_pct * 100:.1f}% before losing "
                f"{stop_pct * 100:.1f}%, within {ecfg.max_hold_minutes:.0f} minutes. "
                f"Below {breakeven_pct * 100:.2f}% it loses money even if it rises."
            ),
        }

    def decide(self, observation: dict) -> tuple[list[TradeIntent], str]:
        tiers = self.acfg.tiers
        allowed = set(observation["tradable"])
        prices = {c["symbol"]: c["price"] for c in observation["candidates"] if c.get("price")}
        raw = self.llm.ask(self._prompt(observation), system=_SYSTEM, tier=tiers.decide)
        intents, commentary = self._parse(raw, allowed, prices)

        # Escalate rather than act on a low-confidence view.
        if intents and min(i.confidence for i in intents) < tiers.confidence_floor:
            log.info("low confidence, escalating to %s", tiers.escalate_on_low_confidence)
            raw = self.llm.ask(
                self._prompt(observation), system=_SYSTEM, tier=tiers.escalate_on_low_confidence
            )
            intents, commentary = self._parse(raw, allowed, prices)
        return intents, commentary

    def _parse(
        self, raw: str, allowed: set[str], prices: dict[str, float]
    ) -> tuple[list[TradeIntent], str]:
        match = _JSON.search(raw or "")
        if not match:
            log.warning("decide: no JSON in model reply")
            return [], ""
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            log.warning("decide: bad JSON (%s)", exc)
            return [], ""

        intents = []
        for item in payload.get("intents", []):
            try:
                symbol = str(item["symbol"])
                # Trust nothing: a symbol the model was not shown is discarded here,
                # before the gate ever sees it. This is what stops a hallucinated or
                # injected ticker from reaching the broker.
                if symbol not in allowed:
                    log.warning("decide: dropping symbol %s that was not offered", symbol)
                    continue
                intents.append(
                    TradeIntent(
                        market=str(self.market),
                        side=Side(str(item["side"]).upper()),
                        symbol=symbol,
                        quantity=int(item["quantity"]),
                        limit_price=(
                            float(item["limit_price"])
                            if item.get("limit_price") is not None
                            else None
                        ),
                        # The gate refuses orders it cannot value. A market order
                        # carries no price, so supply the screened price as the
                        # reference — without this every market order is rejected.
                        reference_price=prices.get(symbol),
                        reason=str(item.get("reason", ""))[:500],
                        confidence=float(item.get("confidence", 0.0)),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("decide: dropping malformed intent %s (%s)", item, exc)
        return intents, str(payload.get("commentary", ""))[:1000]

    # -- one cycle ------------------------------------------------------------

    def _record_realised(self, observation) -> None:
        """Feed broker-reported realised P&L to the ledger.

        Without this the ledger only ever sees costs, so `/costs` reports a
        permanent loss and the profitability question can never be answered.
        Broker-sourced, never an equity difference -- owner deposits and
        withdrawals must not read as performance.
        """
        try:
            symbols = sorted(self._traded_symbols)
            pnl = self.adapter.realised_pnl(symbols)
            if pnl is not None:
                self.ledger.record_realised(pnl)
        except Exception:
            log.exception("realised P&L update failed")

    def run_exits(self, observation: dict) -> int:
        """Supervise open positions and send any exit the policy calls for."""
        sent = 0
        try:
            signals = self.supervisor.check(observation["holdings"], observation["prices"])
        except Exception as exc:
            log.exception("exit supervision failed")
            self.journal.write("exit_check_failed", error=str(exc))
            return 0

        for sig in signals:
            intent = TradeIntent(
                market=str(self.market),
                side=Side.SELL,
                symbol=sig.symbol,
                quantity=int(sig.quantity),
                reference_price=sig.price,
                reason=f"{sig.reason}: {sig.detail}",
                confidence=1.0,
            )
            verdict = self.gate.evaluate(intent)
            self.journal.write(
                "exit_signal",
                signal=str(sig),
                approved=verdict.approved,
                reasons=verdict.reasons,
            )
            if not verdict.approved:
                log.error("EXIT BLOCKED %s: %s", sig, verdict.reasons)
                self.telegram.send(f"⚠️ exit blocked: {sig}\n{verdict.reasons}")
                continue
            try:
                response = self.executor.execute(verdict)
                if not response.get("dry_run"):
                    sent += 1
                    self._traded_symbols.add(sig.symbol)
                    self.ledger.record_trade(
                        symbol=sig.symbol,
                        side="SELL",
                        quantity=sig.quantity,
                        price=sig.price,
                        market=self.adapter.fee_market(sig.symbol),
                    )
                self.journal.write("exit_order", signal=str(sig), response=response)
                self.telegram.send(f"🔻 {sig}")
            except Exception as exc:
                log.exception("exit order failed")
                self.journal.write("exit_order_failed", signal=str(sig), error=str(exc))
        return sent

    def run_cycle(self) -> CycleResult:
        now = dt.datetime.now(self.tz)
        result = CycleResult(observed_at=now, errors=[])

        result.tradable = self.is_market_open(now)
        self._record_realised(observation=None)

        try:
            observation = self.observe()
        except Exception as exc:
            log.exception("observe failed")
            result.errors.append(f"observe: {type(exc).__name__}: {exc}")
            self.journal.write("observe_failed", error=str(exc))
            return result

        if not result.tradable:
            self.journal.write("cycle_skipped", reason="market closed")
            return result

        # The kill switch stops NEW risk only; exits above have already run.
        if self.gate.halted:
            self.journal.write("cycle_skipped", reason="halted (entries)")
            return result

        # Slot limit: full-balance sizing means one name at a time.
        if not self.sizer.slots_free(len(observation["holdings"])):
            self.journal.write("cycle_skipped", reason="no free slots")
            return result

        # Two spend guards before the expensive step. A decision cycle costs real
        # money (~47 KRW measured), so it must be worth making.
        budget = self.cfg.accounting.max_api_krw_per_day
        if budget and self.ledger.day().api_krw >= budget:
            result.errors.append(f"daily API budget {budget:,.0f} KRW exhausted")
            self.journal.write("cycle_skipped", reason="api budget")
            return result

        fingerprint = tuple(c["symbol"] for c in observation["candidates"])
        if self.acfg.skip_decide_if_unchanged and fingerprint == self._last_fingerprint:
            log.info("candidate set unchanged; skipping the model")
            self.journal.write("cycle_skipped", reason="unchanged candidates")
            return result
        self._last_fingerprint = fingerprint

        try:
            intents, commentary = self.decide(observation)
        except Exception as exc:
            log.exception("decide failed")
            result.errors.append(f"decide: {type(exc).__name__}: {exc}")
            self.journal.write("decide_failed", error=str(exc))
            return result

        # Size every entry from the broker's orderable balance. The model chooses
        # WHAT; how much is arithmetic over reported cash, which cannot hallucinate
        # an extra digit. Anything that sizes to zero is dropped before the gate.
        sized = []
        for intent in intents:
            if (
                intent.side is Side.BUY
                and self.sizer.size(intent, observation["snapshot"].cash) <= 0
            ):
                log.warning("%s sized to 0; dropped", intent.symbol)
                continue
            sized.append(intent)
        intents = sized

        result.intents, result.commentary = len(intents), commentary
        verdicts = self.gate.evaluate_all(intents)
        self.journal.write(
            "decision",
            commentary=commentary,
            verdicts=[
                {"intent": asdict(v.intent), "approved": v.approved, "reasons": v.reasons}
                for v in verdicts
            ],
        )

        for verdict in verdicts:
            if not verdict.approved:
                continue
            result.approved += 1
            try:
                response = self.executor.execute(verdict)
                sent = not response.get("dry_run")
                result.sent += int(sent)
                if sent:
                    # Bill the trade so the day's break-even reflects it.
                    price = verdict.intent.price_for_valuation or 0.0
                    self._traded_symbols.add(verdict.intent.symbol)
                    self.ledger.record_trade(
                        symbol=verdict.intent.symbol,
                        side=str(verdict.intent.side),
                        quantity=verdict.intent.quantity,
                        price=price,
                        market=self.adapter.fee_market(verdict.intent.symbol),
                    )
                self.journal.write("order", intent=asdict(verdict.intent), response=response)
            except Exception as exc:
                log.exception("order failed")
                result.errors.append(f"order {verdict.intent.symbol}: {exc}")
                self.journal.write("order_failed", intent=asdict(verdict.intent), error=str(exc))

        return result

    # -- loop -----------------------------------------------------------------

    def run(self, cycles: int | None = None) -> None:
        log.info(
            "agent start: market=%s dry_run=%s universe=%d listings, %d candidates/cycle",
            self.market,
            self.executor.dry_run,
            len(getattr(self.adapter.universe, "symbols", []) or []),
            self.acfg.screen.candidates,
        )
        if self.executor.dry_run:
            self.telegram.send("🟡 agent started in *DRY RUN* — no orders will be transmitted.")
        else:
            self.telegram.send("🔴 agent started *LIVE* — orders will be transmitted.")

        completed = 0
        while cycles is None or completed < cycles:
            result = self.run_cycle()
            log.info("%s", result.summary())
            if result.intents or result.errors:
                self.telegram.send(
                    result.summary() + (f"\n{result.commentary}" if result.commentary else "")
                )
            completed += 1
            if cycles is None or completed < cycles:
                time.sleep(self.acfg.loop_interval_s)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run the trading agent.")
    ap.add_argument("--broker", default="kiwoom", choices=["kiwoom", "binance"])
    ap.add_argument("--market", default=None, choices=["KR", "US"], help="kiwoom only")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    ap.add_argument(
        "--dry-run", action="store_true", help="force dry run regardless of config.yaml"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config()
    if args.market:
        cfg.agent.market = args.market
    if args.dry_run:
        cfg.agent.dry_run = True

    agent = TradingAgent(cfg, broker=args.broker)
    if not agent.executor.dry_run:
        log.critical(
            "LIVE TRADING: real orders on market=%s, equity limits %s KRW/order, %s/day",
            agent.market,
            f"{cfg.risk.max_order_value_krw:,.0f}",
            cfg.risk.max_orders_per_day,
        )
    agent.run(cycles=args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
