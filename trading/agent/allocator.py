"""The capital allocator: the model's share of the book, earned automatically.

    uv run python -m trading.agent.allocator

**The owner's instruction (2026-09-07): the model's portion must RISE as
measured profit arrives, automatically, up to 85%.** This module is that
sentence as arithmetic — the inner loop's counterpart to the mainnet gate.
Where `promotion.py` asks "may we leave the testnet yet?", this asks "how much
of the book has the model earned today?" and answers it every hour without a
human editing `explore.entry_pct` by hand.

ONE number does the work. `model_share` ramps from `base_share` to `max_share`
as evidence accumulates, and both exploration knobs are derived from it:

    explore.entry_pct      = 1 - model_share      (how OFTEN the dice enter)
    explore.max_positions  = slots x (1 - share)  (how MUCH the dice may hold)

That identity is why the ramp needs no second calibration: at the historical
`base_share` 0.50 it reproduces `entry_pct: 0.5`, and at `max_share` 0.85 it
lands exactly on the `floor_pct: 0.15` the config has always documented as the
manual decay target. The manual schedule became a computed one; the endpoints
did not move.

**Why ε can never reach zero.** `max_share` caps the ramp below 1.0 by
construction, so the random arm always keeps a real seat. That is the standing
invariant — without a live random arm, model-vs-chance stops being measurable
— and here it is enforced in code rather than in a comment.

**Why shifting capital does not blind the gate.** The paired model-vs-shadow
corpus is produced per DECISION (`shadow_random`, journalled on every decide
reply), not by the random ENTRY arm. So handing slots to the model slows the
gate's control group not at all: the two random mechanisms are independent, and
only the one that spends money is throttled. This is the fact that makes an
automatic ramp safe, and it is the first thing to re-check if either is moved.

**The driver is asymmetric, deliberately.**

- *Upward* requires BOTH realised profit and a demonstrated edge over chance
  (`quality = min(profit_score, edge_score)`), because this repository has
  already measured what raw profit alone proves: a long-only rule inside a
  +96% window looked brilliant and still lost to buy-and-hold. Profit in a
  rising market is not evidence that the model is picking well, so it cannot
  buy the model more capital on its own. The edge term reads the bootstrap CI's
  LOWER bound, not the point estimate — the same anti-luck standard the gate
  applies.
- *Downward* is driven by realised profit ALONE, because losing money is
  self-evident and needs no significance test to act on.

Both directions are scaled by `confidence` (how much evidence exists at all)
and rate-limited by `max_step`, so a lucky week cannot hand over the book.
Nothing here is a ratchet: unlike a stop, this must fall back when the model
stops earning.

The allocator changes only WHO PROPOSES, never what disposes. The risk gate,
the sizer and the exit supervisor are untouched, so invariant 3 holds: an LLM
is still never the last thing before an order.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trading.config import AppConfig, config


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class Allocation:
    """One allocation decision, with the arithmetic that produced it.

    Every field is journalled. Exploration updates a ledger, not a policy —
    the same discipline the random arm follows, applied to the thing that
    now moves capital automatically.
    """

    model_share: float
    explore_entry_pct: float
    explore_max_positions: int
    # -- the arithmetic, kept for the audit trail --
    strength: float = 0.0
    confidence: float = 0.0
    quality: float = 0.0
    profit_score: float = 0.0
    edge_score: float = 0.0
    penalty: float = 0.0
    previous_share: float | None = None
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"model {self.model_share:.0%} of the book "
            f"(explore: {self.explore_entry_pct:.0%}/cycle, "
            f"≤{self.explore_max_positions} concurrent) — " + "; ".join(self.reasons)
        )


def plan(
    metrics: dict,
    cfg: AppConfig | None = None,
    previous_share: float | None = None,
) -> Allocation:
    """Compute the model's earned share of the book. Pure — no I/O.

    `metrics` is `promotion.evaluate()["metrics"]`: the same measured record
    the mainnet gate reads, so the inner loop and the outer verdict can never
    disagree about what the model has earned.
    """
    cfg = cfg or config()
    a = cfg.allocator
    p = cfg.promotion

    n_trips = int(metrics.get("n_trips") or 0)
    pair_n = int(metrics.get("pair_n") or 0)
    avg_net_pct = metrics.get("avg_net_pct")
    edge_lower = metrics.get("edge_lower")
    ready_buckets = int(metrics.get("ready_buckets") or 0)

    reasons: list[str] = []

    # -- confidence: is there enough evidence to move capital at all? --------
    # THE SYSTEM IS THE LEARNER, NOT THE MODEL (owner, 2026-09-07). This is
    # context RL: the weights are frozen, and policy improvement IS the growth
    # of the measured record the model reads at decision time. So the capital
    # share tracks what the CONTEXT knows, not what the LLM is:
    #   - trips        — the money is real
    #   - pairs        — the comparison is real
    #   - ready buckets — the RAG can actually say something at decision time
    # The weakest governs, because a policy is only as good as the thinnest
    # evidence behind it. An empty store contributes silence to the prompt;
    # it must likewise buy no capital.
    trip_conf = _clamp(n_trips / p.min_closed_trades, 0.0, 1.0) if p.min_closed_trades else 1.0
    pair_conf = _clamp(pair_n / p.min_shadow_pairs, 0.0, 1.0) if p.min_shadow_pairs else 1.0
    rag_conf = _clamp(ready_buckets / a.min_ready_buckets, 0.0, 1.0) if a.min_ready_buckets else 1.0
    confidence = min(trip_conf, pair_conf, rag_conf)

    # -- profit: the owner's stated driver -----------------------------------
    profit_score = 0.0
    penalty = 0.0
    if avg_net_pct is not None and a.target_net_pct > 0:
        if avg_net_pct >= 0:
            profit_score = _clamp(avg_net_pct / a.target_net_pct, 0.0, 1.0)
        else:
            # Losing money needs no significance test. The share falls.
            penalty = _clamp(-avg_net_pct / a.target_net_pct, 0.0, 1.0)

    # -- edge: has the model beaten chance, ruling out luck? -----------------
    edge_score = 0.0
    if edge_lower is not None and a.target_edge_pct > 0:
        edge_score = _clamp(edge_lower / a.target_edge_pct, 0.0, 1.0)

    quality = min(profit_score, edge_score)
    strength = confidence * quality

    base = a.base_share
    prev = base if previous_share is None else previous_share

    if penalty > 0:
        target = base - (base - a.min_share) * penalty * confidence
        reasons.append(
            f"avg net {avg_net_pct:+.3f}%/trip is negative → giving the book back to the dice"
        )
    else:
        target = base + (a.max_share - base) * strength
        if strength <= 0:
            if confidence < 1.0:
                reasons.append(
                    f"evidence still thin (trips {n_trips}/{p.min_closed_trades}, "
                    f"pairs {pair_n}/{p.min_shadow_pairs}, "
                    f"RAG buckets {ready_buckets}/{a.min_ready_buckets})"
                )
            if edge_score <= 0:
                reasons.append(
                    "no demonstrated edge over chance yet"
                    + (f" (CI lower bound {edge_lower:+.2f}%)" if edge_lower is not None else "")
                )
            if not reasons:
                reasons.append("holding at base")
        else:
            reasons.append(
                f"profit {profit_score:.2f} x edge {edge_score:.2f} x "
                f"confidence {confidence:.2f} → strength {strength:.2f}"
            )

    # Rate limit: a good week may nudge the book, never take it.
    if a.max_step > 0:
        target = _clamp(target, prev - a.max_step, prev + a.max_step)
    # The hard rails. max_share below 1.0 is what keeps ε alive forever.
    share = _clamp(target, a.min_share, a.max_share)

    # -- derive both exploration knobs from the one number -------------------
    entry_pct = _clamp(1.0 - share, 0.0, 1.0)
    slots = cfg.sizing.max_positions
    if slots:
        explore_max = max(a.min_explore_positions, round(slots * (1.0 - share)))
        explore_max = min(explore_max, slots)
    else:
        explore_max = cfg.explore.max_positions

    return Allocation(
        model_share=share,
        explore_entry_pct=entry_pct,
        explore_max_positions=explore_max,
        strength=strength,
        confidence=confidence,
        quality=quality,
        profit_score=profit_score,
        edge_score=edge_score,
        penalty=penalty,
        previous_share=prev,
        reasons=reasons,
    )


class Allocator:
    """Interval-gated allocation with a persisted share, for the live loop.

    The share is persisted so a restart resumes the ramp instead of snapping
    back to base — the allocation is a measured position, not session state.
    """

    def __init__(self, cfg: AppConfig | None = None, journal=None):
        self.cfg = cfg or config()
        self.journal = journal
        self._path = Path(self.cfg.allocator.state)
        self._last_run: dt.datetime | None = None
        self._current: Allocation | None = None

    # -- persistence ---------------------------------------------------------

    def _load_share(self) -> float | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            share = data.get("model_share")
            return float(share) if share is not None else None
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, alloc: Allocation) -> None:
        payload = asdict(alloc)
        payload["ts"] = dt.datetime.now(dt.UTC).isoformat()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass  # an audit-write failure must never abort a cycle

    # -- the live surface ----------------------------------------------------

    def _disabled(self) -> Allocation:
        """The off switch: hand the operator's tuned config straight back.

        Checked by BOTH `current` and `maybe_run`. Checking it in only one was
        a real defect — `run_explore` reads `current`, so an allocator switched
        off would still have overridden the hand-tuned `explore.*` values it
        exists to defer to.
        """
        return Allocation(
            model_share=1.0 - self.cfg.explore.entry_pct,
            explore_entry_pct=self.cfg.explore.entry_pct,
            explore_max_positions=self.cfg.explore.max_positions,
            reasons=["allocator disabled; config values in force"],
        )

    @property
    def current(self) -> Allocation:
        """The allocation in force. Falls back to base before the first run."""
        if not self.cfg.allocator.enabled:
            return self._disabled()
        if self._current is None:
            a = self.cfg.allocator
            share = self._load_share()
            self._current = plan(
                {}, self.cfg, previous_share=share if share is not None else a.base_share
            )
            if share is not None:
                # Honour the persisted share verbatim on the first read; the
                # next interval recomputes it against fresh metrics.
                self._current = Allocation(
                    model_share=share,
                    explore_entry_pct=_clamp(1.0 - share, 0.0, 1.0),
                    explore_max_positions=self._current.explore_max_positions,
                    previous_share=share,
                    reasons=["restored from disk"],
                )
        return self._current

    def maybe_run(self, now: dt.datetime | None = None) -> Allocation:
        """Recompute on the configured interval; otherwise return what stands."""
        a = self.cfg.allocator
        if not a.enabled:
            return self._disabled()
        now = now or dt.datetime.now(dt.UTC)
        due = self._last_run is None or (now - self._last_run) >= dt.timedelta(
            minutes=a.interval_minutes
        )
        if not due:
            return self.current
        self._last_run = now

        from trading.agent.promotion import evaluate

        try:
            metrics = evaluate(self.cfg).get("metrics", {})
        except Exception:  # noqa: BLE001 - measurement must never abort a trading cycle
            return self.current

        previous = self.current.model_share
        alloc = plan(metrics, self.cfg, previous_share=previous)
        self._current = alloc
        self._save(alloc)
        if self.journal is not None and abs(alloc.model_share - previous) > 1e-9:
            self.journal.write(
                "allocation",
                **{k: v for k, v in asdict(alloc).items()},
            )
        return alloc


def render(cfg: AppConfig | None = None) -> str:
    """The allocator's own report — what the model has earned, and why."""
    cfg = cfg or config()
    from trading.agent.promotion import evaluate

    metrics = evaluate(cfg).get("metrics", {})
    alloc = Allocator(cfg).current
    nxt = plan(metrics, cfg, previous_share=alloc.model_share)
    a = cfg.allocator
    lines = [
        "*Capital allocation* (the model's share is earned, and rises automatically)",
        (
            f"  in force: model {alloc.model_share:.0%} · explore "
            f"{alloc.explore_entry_pct:.0%}/cycle, ≤{alloc.explore_max_positions} concurrent"
        ),
        (
            f"  next    : model {nxt.model_share:.0%} "
            f"(band {a.min_share:.0%}..{a.max_share:.0%}, step ≤{a.max_step:.0%})"
        ),
        (
            f"  profit {nxt.profit_score:.2f} · edge {nxt.edge_score:.2f} · "
            f"confidence {nxt.confidence:.2f} → strength {nxt.strength:.2f}"
        ),
    ]
    lines.extend(f"  ↳ {r}" for r in nxt.reasons)
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
