"""Broker state contracts, shared by every adapter.

**Broker records are the single source of truth.** Positions, cash and open
orders are always answered by a live broker read, never by locally accumulated
state. Local persistence exists for audit and analytics only; if it ever
disagrees with the broker, the broker is right and the local copy is a bug.

That rule is why :class:`Snapshot` has no setters and no local mutation of
holdings. The agent may cache a snapshot for at most ``state.max_staleness_s``
seconds to avoid hammering the API within one reasoning cycle, and
``state.reconcile_before_order`` forces a fresh read before anything is sent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


class StaleStateError(RuntimeError):
    """Raised when an order is attempted against a snapshot that was not reconciled."""


class OrderRejected(RuntimeError):
    """Raised when the risk gate refuses an intent handed to an executor."""


@dataclass(slots=True)
class Snapshot:
    """A point-in-time copy of broker-held state. Read-only by construction."""

    market: str
    taken_at: dt.datetime  # timezone-aware (UTC); ages are compared, not displayed
    positions: dict = field(default_factory=dict)
    cash: dict = field(default_factory=dict)
    open_orders: dict = field(default_factory=dict)
    evaluation: dict = field(default_factory=dict)

    def age_s(self, now: dt.datetime | None = None) -> float:
        return ((now or dt.datetime.now(dt.UTC)) - self.taken_at).total_seconds()

    def is_fresh(self, max_staleness_s: float, now: dt.datetime | None = None) -> bool:
        return self.age_s(now) <= max_staleness_s
