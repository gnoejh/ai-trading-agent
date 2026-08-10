"""Account state, read from the broker.

**Broker records are the single source of truth.** Positions, cash and open
orders are always answered by a live broker read, never by locally accumulated
state. Local persistence exists for audit and analytics only; if it ever
disagrees with the broker, the broker is right and the local copy is a bug.

That rule is why this module has no setters and no local mutation of holdings.
The agent may cache a snapshot for at most ``state.max_staleness_s`` seconds to
avoid hammering the API within one reasoning cycle, and
``state.reconcile_before_order`` forces a fresh read before anything is sent.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from trading.brokers.kiwoom.client import KiwoomClient

log = logging.getLogger(__name__)


class StaleStateError(RuntimeError):
    """Raised when an order is attempted against a snapshot that was not reconciled."""


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


class AccountState:
    """Broker-backed account state for one market."""

    def __init__(self, client: KiwoomClient):
        self.client = client
        self.endpoints = client.market_cfg.state
        self.state_cfg = client.cfg.state
        self._snapshot: Snapshot | None = None

    # -- single reads ---------------------------------------------------------

    def _read(self, endpoint, body: dict | None = None) -> dict:
        """Call a configured state endpoint, merging its default discriminators."""
        return self.client.call(endpoint.api_id, {**endpoint.params, **(body or {})}).body

    def positions(self, body: dict | None = None) -> dict:
        return self._read(self.endpoints.positions, body)

    def cash(self, body: dict | None = None) -> dict:
        return self._read(self.endpoints.cash, body)

    def open_orders(self, body: dict | None = None) -> dict:
        return self._read(self.endpoints.open_orders, body)

    def fills(self, body: dict | None = None) -> dict:
        return self._read(self.endpoints.fills, body)

    def evaluation(self, body: dict | None = None) -> dict:
        return self._read(self.endpoints.evaluation, body)

    # -- snapshots ------------------------------------------------------------

    def refresh(self, **bodies: dict) -> Snapshot:
        """Read every state endpoint from the broker and cache the result."""
        self._snapshot = Snapshot(
            market=str(self.client.market),
            taken_at=dt.datetime.now(dt.UTC),
            positions=self.positions(bodies.get("positions")),
            cash=self.cash(bodies.get("cash")),
            open_orders=self.open_orders(bodies.get("open_orders")),
            evaluation=self.evaluation(bodies.get("evaluation")),
        )
        return self._snapshot

    def current(self, **bodies: dict) -> Snapshot:
        """Return a snapshot, re-reading if the cached one is past max staleness."""
        if self._snapshot is None or not self._snapshot.is_fresh(self.state_cfg.max_staleness_s):
            return self.refresh(**bodies)
        return self._snapshot

    def reconcile(self, **bodies: dict) -> Snapshot:
        """Force a fresh broker read. Call before acting on state."""
        return self.refresh(**bodies)

    def assert_reconciled(self) -> Snapshot:
        """Guard for the order path: fail loudly rather than trade on stale state."""
        if self.state_cfg.reconcile_before_order:
            return self.reconcile()
        snap = self._snapshot
        if snap is None:
            raise StaleStateError("no broker snapshot; call reconcile() before ordering")
        if not snap.is_fresh(self.state_cfg.max_staleness_s):
            raise StaleStateError(
                f"snapshot is {snap.age_s():.1f}s old, limit is {self.state_cfg.max_staleness_s}s"
            )
        return snap
