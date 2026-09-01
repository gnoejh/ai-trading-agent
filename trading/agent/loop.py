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
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.accounting.costs import CostLedger
from trading.agent.journal import Journal
from trading.agent.scorer import ExperienceScorer, experience_block
from trading.brokers.adapters import build_adapter
from trading.config import AppConfig, config
from trading.llm.client import LLMClient
from trading.notify.telegram import TelegramNotifier
from trading.risk.exits import PositionSupervisor
from trading.risk.gate import RiskGate, Side, TradeIntent
from trading.risk.sizing import PositionSizer

log = logging.getLogger(__name__)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _num(value) -> float:
    """Broker numerics may arrive as strings; unparseable -> 0.0."""
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


_SYSTEM = """You are the entry selector for an automated trading system.

You are NOT choosing "a good stock". Once you pick, the position is managed
mechanically and you have no further say. Concretely, your pick will be:

  - bought with the configured budget for one position, laddered over several price levels
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
 "best_candidate": {"symbol": "...", "confidence": 0.0-1.0},
 "commentary": "one or two sentences"}

- `best_candidate` is ALWAYS required, even when `intents` is empty: the single
  most promising name on the menu right now, with your honest probability that
  it reaches the target before the stop. It is never traded — it exists so your
  selection skill is measured against a random pick on every decision, not only
  on the rare cycles you trade. Declining to trade while still naming your best
  candidate is the expected common case.

- `confidence` is your estimate of the probability that the trade reaches target
  before stop. 0.5 means a coin flip. Be honest: below the floor in `trade_rules`
  the decision is escalated to a stronger model rather than acted on, so
  overstating it removes a safety net rather than helping the trade.
- An empty list is a valid and often correct answer. Costs are paid on every
  trade; doing nothing is free.
- Only symbols from `candidates`. Anything else is discarded.
- `quantity` is ignored for buys — size is computed from the balance.
- Never propose selling more than the reported holding."""


def build_trade_rules(cfg: AppConfig, market: str, ledger: CostLedger) -> dict:
    """The exit contract every pick is judged by, in the model's terms.

    Module-level because the historical replay harness must ask the model the
    IDENTICAL question the live loop asks — a backtest against a different
    payoff contract measures a different trader.
    """
    ecfg = cfg.exits.for_market(market)
    hurdle = ledger.breakeven_move_pct(market)
    stop_pct = ecfg.stop_loss_pct
    # Mirrors ExitPolicy: target is the wider of the hurdle multiple and the
    # minimum reward:risk, measured from NET break-even.
    breakeven_pct = hurdle
    risk_pct = breakeven_pct + stop_pct
    target_pct = max(
        (1 + breakeven_pct) * (1 + ecfg.target_hurdle_multiple * hurdle) - 1,
        breakeven_pct + ecfg.min_reward_risk * risk_pct,
    )
    # Break-even win rate implied by the payoff. A 0.60 floor against a 1.8:1
    # trade demands near-certainty for a bet that pays above 36% -- which is
    # why the trader kept declining candidates it plainly liked.
    breakeven_wr = risk_pct / (target_pct + risk_pct) if (target_pct + risk_pct) else 0.5
    return {
        "round_trip_cost_pct": round(hurdle * 100, 3),
        "breakeven_win_rate_pct": round(breakeven_wr * 100, 1),
        "breakeven_move_pct": round(breakeven_pct * 100, 3),
        "target_gain_pct": round(target_pct * 100, 2),
        "stop_loss_pct": round(stop_pct * 100, 2),
        "reward_risk": round((target_pct - breakeven_pct) / risk_pct, 2),
        "max_hold_minutes": ecfg.max_hold_minutes,
        "confidence_floor": cfg.agent.tiers.confidence_floor,
        "note": (
            f"A pick must gain {target_pct * 100:.1f}% before losing "
            f"{stop_pct * 100:.1f}%, within {ecfg.max_hold_minutes:.0f} minutes. "
            f"Below {breakeven_pct * 100:.2f}% it loses money even if it rises. "
            f"This payoff is PROFITABLE above a {breakeven_wr * 100:.0f}% hit rate -- "
            f"you do not need to be confident of winning, only better than that."
        ),
    }


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
        broker: str = "binance",
        adapter=None,
    ):
        self.cfg = cfg or config()
        self.acfg = self.cfg.agent
        self.broker = broker
        bcfg = getattr(self.cfg.broker, broker, self.cfg.broker.binance)
        self.tz = ZoneInfo(getattr(bcfg, "timezone", "Asia/Seoul"))

        self.adapter = adapter or build_adapter(self.broker, self.acfg.market, self.cfg)
        self.market = self.adapter.market
        self.state = self.adapter.state()
        self.gate = RiskGate(
            self.state,
            self.cfg,
            market=self.market,
            # Flat symbols only: an open position's buys are not realised losses,
            # and the loss cap must not trip on cash merely committed.
            pnl_provider=self._flat_realised_pnl,
        )
        self.executor = self.adapter.executor(self.gate)
        self.sizer = PositionSizer(self.cfg)
        self.supervisor = PositionSupervisor(
            self.state, self.cfg, market=self.market, is_dust=self._order_dust
        )
        self.ledger = CostLedger(self.cfg)
        self.llm = LLMClient(self.cfg, ledger=self.ledger)
        # One journal per venue: the scorer resolves observations against the
        # venue's own price source, so venues must never share a decision file.
        # The bare configured name stays Binance's for continuity; Kiwoom gets
        # one per market surface (journal.kiwoom.KR.jsonl / .US.jsonl).
        journal_path = Path(self.acfg.journal)
        if broker != "binance":
            journal_path = journal_path.with_name(
                f"{journal_path.stem}.{broker}.{self.acfg.market}{journal_path.suffix}"
            )
        self.journal = Journal(str(journal_path))
        self.telegram = notifier or TelegramNotifier()
        self._last_fingerprint: tuple[str, ...] | None = None
        # Symbols this process has traded today -- Binance's myTrades needs a
        # symbol, so P&L is only queried where something actually happened.
        self._traded_symbols: set[str] = set()
        # Exploration arm: one seeded stream drives both the shadow pick and the
        # random entries, so a fixed seed reproduces the whole sequence.
        self._rng = random.Random(self.cfg.explore.seed or None)
        # Random-arm entries opened by THIS process. Lost on restart, which only
        # loosens the explore cap for one session -- the journal keeps the truth.
        self._random_positions: set[str] = set()
        self.scorer: ExperienceScorer | None = None
        if self.broker == "binance" and self.cfg.score.enabled:
            self.scorer = ExperienceScorer(
                self.adapter.client, self.adapter.screen, self.ledger, self.cfg
            )

    # -- clock ----------------------------------------------------------------

    def is_market_open(self, now: dt.datetime | None = None) -> bool:
        """Is *this agent's* market trading now? Crypto never closes, so this is
        true whenever the market is listed in `agent.always_open`."""
        return self.acfg.is_open(str(self.market), now)

    # -- observe --------------------------------------------------------------

    def observe(self) -> dict:
        """Broker state, screened candidates, and quotes for those candidates only.

        Quotes are fetched for the screen output, not the full ~2,650-name universe:
        one call per candidate is affordable, one per listing is not.
        """
        snapshot = self.state.reconcile()
        if hasattr(self.adapter, "normalise"):
            self.adapter.normalise(snapshot)
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
        # Managed holdings only. The raw snapshot carries EVERY balance -- 482
        # seed rows on testnet -- which blew the payload past the 20k truncation
        # guard and silently cut trade_rules off the END of the prompt: the
        # model spent cycles declining with "trade_rules not supplied" while
        # the journal recorded it as considered judgement.
        held = {
            s: {"quantity": h.get("quantity"), "cost_basis": h.get("cost_basis")}
            for s, h in observation["holdings"].items()
            if float(h.get("cost_basis") or 0) > 0
        }
        return json.dumps(
            {
                # Critical fields FIRST: if the payload ever overflows the guard
                # again, truncation must eat detail, never the contract.
                #
                # The payoff structure the pick will actually be held to. Without
                # this the model is asked "which is good?" when the real question
                # is "which reaches +X% before -Y% within Z minutes, net of costs?"
                # -- a different and far more answerable question.
                "trade_rules": self._trade_rules(),
                # 0 means UNLIMITED to the gate, but a model shown a bare 0 reads
                # it as "nothing is allowed" and declines to trade. Observed live
                # 2026-08-10: "risk limits are all zero, making any order likely to
                # be rejected". Render the meaning, never the raw sentinel.
                "limits": _describe_limits(self.cfg.risk),
                # The system's own measured record (the experience RAG). Renders
                # only buckets that cleared the sample-size gate; absent entirely
                # while the store is unfilled -- silence, never fabricated priors.
                **({"measured_record": record} if (record := experience_block(self.cfg)) else {}),
                "candidates": observation["candidates"],
                "cash": {k: v for k, v in snap.cash.items() if not isinstance(v, list | dict)},
                "holdings": held,
                "unmanaged_balances": len(observation["holdings"]) - len(held),
                "open_orders": snap.open_orders,
                **({"quotes": compact_quotes} if compact_quotes else {}),
            },
            ensure_ascii=False,
            default=str,
        )[:20000]

    def _trade_rules(self) -> dict:
        return build_trade_rules(self.cfg, str(self.market), self.ledger)

    def decide(self, observation: dict) -> tuple[list[TradeIntent], str, str | None]:
        tiers = self.acfg.tiers
        allowed = set(observation["tradable"])
        prices = {c["symbol"]: c["price"] for c in observation["candidates"] if c.get("price")}
        raw = self.llm.ask(self._prompt(observation), system=_SYSTEM, tier=tiers.decide)
        intents, commentary, best = self._parse(raw, allowed, prices)

        # Escalate rather than act on a low-confidence view.
        if intents and min(i.confidence for i in intents) < tiers.confidence_floor:
            log.info("low confidence, escalating to %s", tiers.escalate_on_low_confidence)
            raw = self.llm.ask(
                self._prompt(observation), system=_SYSTEM, tier=tiers.escalate_on_low_confidence
            )
            intents, commentary, best = self._parse(raw, allowed, prices)
        return intents, commentary, best

    def _parse(
        self, raw: str, allowed: set[str], prices: dict[str, float]
    ) -> tuple[list[TradeIntent], str, str | None]:
        match = _JSON.search(raw or "")
        if not match:
            log.warning("decide: no JSON in model reply")
            return [], "", None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            log.warning("decide: bad JSON (%s)", exc)
            return [], "", None

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

        # The virtual pick: the model's top-ranked candidate, present on declines
        # too. Same anti-hallucination rule as intents — off the menu, discarded.
        best = None
        best_raw = payload.get("best_candidate")
        if isinstance(best_raw, dict):
            candidate = str(best_raw.get("symbol", ""))
            if candidate in allowed:
                best = candidate
            elif candidate:
                log.warning("decide: dropping best_candidate %s that was not offered", candidate)
        return intents, str(payload.get("commentary", ""))[:1000], best

    # -- one cycle ------------------------------------------------------------

    def _flat_traded_symbols(self) -> list[str]:
        """Symbols this process traded today that the account no longer holds.

        Binance reconstructs realised P&L from myTrades cash flow, which is only
        exact for a FLAT symbol -- an open position reads as pure outflow. During
        a many-position day that made "realised P&L" mostly measure cash
        committed, and pushed the daily-loss check toward tripping on phantom
        losses. So both the ledger and the loss cap only ever see flat symbols.
        """
        try:
            snap = self.state.current()
            held = {
                str(row.get("stk_cd"))
                for row in snap.positions.get("rows", [])
                if _num(row.get("rmnd_qty")) > 0
            }
        except Exception:  # noqa: BLE001 - an unreadable account means no symbols are provably flat
            return []
        return sorted(self._traded_symbols - held)

    def _flat_realised_pnl(self) -> float | None:
        """Broker-reported realised P&L over today's closed symbols."""
        return self.adapter.realised_pnl(self._flat_traded_symbols())

    def _record_realised(self, observation) -> None:
        """Feed broker-reported realised P&L to the ledger.

        Without this the ledger only ever sees costs, so `/costs` reports a
        permanent loss and the profitability question can never be answered.
        Broker-sourced, never an equity difference -- owner deposits and
        withdrawals must not read as performance.
        """
        try:
            pnl = self._flat_realised_pnl()
            if pnl is not None:
                # In the venue's own currency; the ledger converts for the report.
                currency = self.cfg.accounting.fees_for(self.market).currency
                self.ledger.record_realised(pnl, currency=currency)
        except Exception:
            log.exception("realised P&L update failed")

    def _order_dust(self, symbol: str, qty: float, price: float) -> bool:
        """True when a holding of `qty` cannot form a valid order on its venue.

        The supervisor uses this to refuse adopting — and to close — plans that
        could only ever emit refused orders (0.34 PROM under the $5 minNotional,
        live 2026-08-31).
        """
        rules = self.adapter.rules_for(symbol)
        if rules is None:
            return qty < 1  # whole-share venues
        quantized = rules.quantize_qty(qty)
        return quantized <= 0 or rules.rejects(quantized, price) is not None

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
                # Never int(): Binance quantities are fractional, and int()
                # truncated a 0.34-PROM exit to quantity 0 -- refused by the
                # gate every cycle. The executor quantizes per venue.
                quantity=sig.quantity,
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

    @staticmethod
    def _managed_symbols(holdings: dict) -> set[str]:
        """Symbols this system (or its owner) actually paid for.

        A balance with no cost basis is not a position in any sense that should
        gate a pick: the supervisor refuses to manage it and no exit will ever
        free its "slot". Filtering picks on RAW balances excluded every seeded
        symbol — the shadow pick was always None and the random arm sampled
        only coins too new to be seeded (observed 2026-08-31: 48 decisions,
        zero paired comparisons, and explore stuck on the same four listings).
        """
        return {s for s, h in holdings.items() if float(h.get("cost_basis") or 0) > 0}

    @staticmethod
    def _managed_count(holdings: dict) -> int:
        return len(TradingAgent._managed_symbols(holdings))

    def _shadow_pick(self, observation: dict) -> str | None:
        """A random symbol from the same shortlist the model saw. Journal-only.

        Never traded — it exists so every model decision has a paired chance
        baseline resolved over the identical menu and horizon.
        """
        held = self._managed_symbols(observation["holdings"])
        symbols = [c["symbol"] for c in observation["candidates"] if c["symbol"] not in held]
        return self._rng.choice(symbols) if symbols else None

    def run_explore(self, observation: dict, free_slots: int) -> int:
        """One random small entry from the TRADABLE pool — the exploration arm.

        No model call. Random entries fill the experience corpus with ground
        truth the screened arm cannot provide, so they sample the pool with the
        strategy bounds removed; liquidity, lot rules and the stablecoin
        exclusion still apply, and everything downstream is the normal
        machinery — sizer, gate, executor, exit supervisor. Exploration changes
        who proposes, never what disposes.
        """
        ecfg = self.cfg.explore
        if not ecfg.enabled or free_slots <= 0:
            return 0
        screen = getattr(self.adapter, "screen", None)
        if screen is None or not hasattr(screen, "tradable_pool"):
            return 0  # exploration needs a venue that exposes its tradable pool
        # MANAGED positions only: filtering on raw balances excluded every
        # seeded symbol and left the random arm sampling only recent listings.
        held = self._managed_symbols(observation["holdings"])
        batch = min(
            free_slots,
            max(ecfg.entries_per_cycle, 1),
            max(ecfg.max_positions - len(self._random_positions & held), 0),
        )
        if batch <= 0:
            return 0
        if self._rng.random() >= ecfg.entry_pct:
            return 0

        try:
            order_size = self.sizer.budget(observation["snapshot"].cash)
            pool = [e for e in screen.tradable_pool(order_size) if e["symbol"] not in held]
        except Exception as exc:
            log.exception("explore pool failed")
            self.journal.write("explore_failed", error=str(exc))
            return 0

        sent_total = 0
        for _ in range(batch):
            if not pool:
                break
            entry = self._rng.choice(pool)
            pool = [e for e in pool if e["symbol"] != entry["symbol"]]
            sent_total += self._explore_entry(observation, entry)
        return sent_total

    def _explore_entry(self, observation: dict, entry: dict) -> int:
        """Size, gate, execute and journal ONE random entry."""
        intent = TradeIntent(
            market=str(self.market),
            side=Side.BUY,
            symbol=entry["symbol"],
            quantity=0,
            reference_price=entry["price"],
            reason="exploration: random arm",
            confidence=1.0,
        )
        rules = self.adapter.rules_for(entry["symbol"])
        if self.sizer.size(intent, observation["snapshot"].cash, rules) <= 0:
            self.journal.write("explore", entry=entry, sent=False, reasons=["sized to 0"])
            return 0
        verdict = self.gate.evaluate(intent)
        if not verdict.approved:
            self.journal.write("explore", entry=entry, sent=False, reasons=verdict.reasons)
            return 0
        try:
            response = self.executor.execute(verdict)
        except Exception as exc:
            log.exception("explore order failed")
            self.journal.write("explore_failed", entry=entry, error=str(exc))
            return 0
        sent = not response.get("dry_run")
        if sent:
            self._traded_symbols.add(intent.symbol)
            self._random_positions.add(intent.symbol)
            self.ledger.record_trade(
                symbol=intent.symbol,
                side="BUY",
                quantity=intent.quantity,
                price=intent.reference_price or 0.0,
                market=self.adapter.fee_market(intent.symbol),
            )
            self.telegram.send(
                f"🎲 explore BUY {intent.symbol} x{intent.quantity:g} @ {entry['price']:,.6g}"
            )
        self.journal.write(
            "explore", entry=entry, quantity=intent.quantity, sent=sent, response=response
        )
        return int(sent)

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

        # Exits FIRST, and unconditionally: before the halt check, before the API
        # budget, before the unchanged-candidates skip. An open position must be
        # supervised on every pass.
        #
        # This call was silently dropped by a later edit while the comment below
        # kept asserting it ran. Stop, target, trail and time-stop were therefore
        # inert in production for the whole of 2026-08-11, which is the deeper
        # cause of the TUTUSDT loss -- the entry price bug sat downstream of a stop
        # that was never evaluated at all.
        result.exits = self.run_exits(observation)

        # Scoring pass (interval-gated): resolves due observations and rebuilds
        # the experience store. After exits, never before them; independent of
        # every entry guard below, because measuring is not risk.
        if self.scorer:
            self.scorer.maybe_run()

        # The kill switch stops NEW risk only; exits above have already run.
        if self.gate.halted:
            self.journal.write("cycle_skipped", reason="halted (entries)")
            return result

        # Slot limit, shared by the model and the exploration arm. Slots count
        # MANAGED positions -- holdings with a known cost basis -- exactly the
        # set the exit supervisor manages. Counting raw balances filled every
        # slot with the testnet's ~480 seed holdings and silently disabled both
        # entry arms: the cycle reported "no free slots" forever.
        managed = self._managed_count(observation["holdings"])
        free_slots = self.sizer.slots_free(managed)
        if not free_slots:
            self.journal.write("cycle_skipped", reason="no free slots")
            return result

        # Exploration arm BEFORE the model guards: a random entry costs no
        # tokens, so it must still run on the cycles the model skips (budget
        # spent, unchanged candidates) -- those are exactly its cheapest cycles.
        explored = self.run_explore(observation, free_slots)
        result.sent += explored
        if explored:
            # The random entry consumed cash and a slot; the model must not size
            # against the stale snapshot.
            observation["snapshot"] = self.state.reconcile()
            free_slots -= explored
            if not free_slots:
                self.journal.write("cycle_skipped", reason="no free slots after explore")
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
            intents, commentary, best = self.decide(observation)
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
        # The virtual pick: the model's top-ranked candidate on EVERY decision,
        # declines included. Never traded; resolved like the shadow. Without it
        # the model-vs-random corpus grows only on the rare cycles the model
        # trades, and the mainnet gate's slowest criterion waits weeks for an n
        # it could collect in days. Falls back to the first proposed buy for a
        # reply that omits the field.
        virtual = best or next((i.symbol for i in intents if i.side is Side.BUY), None)
        # The decision record carries the MENU, not just the picks: the scorer
        # needs the candidates' features to grade every choice, and the shadow
        # random pick from the same shortlist is the model's paired control.
        self.journal.write(
            "decision",
            commentary=commentary,
            candidates=observation["candidates"],
            shadow_random=self._shadow_pick(observation),
            virtual_pick=virtual,
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
    ap.add_argument("--broker", default="binance", choices=["binance", "kiwoom"])
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
    if args.broker == "kiwoom":
        # PAPER MODE (use_testnet + allow_orders, on reissued 모의투자 keys)
        # trades against the mock host — KR only (`paper_markets`; 모의투자
        # does not serve US). Every other market/configuration is
        # measurement-only: dry_run forced, allow_orders stripped, and
        # use_testnet stripped too so reads and token stay on the mainnet
        # host instead of following the flip to a mock host that cannot
        # serve them.
        if cfg.broker.kiwoom.paper(cfg.agent.market):
            log.info("kiwoom PAPER mode (%s): orders go to the mock host", cfg.agent.market)
        else:
            cfg.agent.dry_run = True
            cfg.broker.kiwoom.use_testnet = False
            cfg.broker.kiwoom.allow_orders = False
            log.info(
                "kiwoom %s is measurement-only: dry_run forced, mainnet reads",
                cfg.agent.market,
            )
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
