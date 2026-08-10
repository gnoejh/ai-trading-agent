"""Render broker account state as a Telegram status report.

Field labels come from the parsed spec, not from hardcoded key names: the vendor
already documents a Korean label for every response element, so a report stays
correct when Kiwoom adds or renames fields. Values are rendered as returned --
Kiwoom sends numbers as sign-prefixed strings, and reformatting them is a place
to introduce errors, so only grouping separators are added.

Everything here reads from :class:`AccountState`, which is broker-backed by
construction, so a status report can never show locally cached fiction.
"""

from __future__ import annotations

import datetime as dt
import logging

from trading.brokers.kiwoom.account import AccountState, Snapshot
from trading.rag.spec_store import SpecStore

log = logging.getLogger(__name__)


def _num(value: str | float | None) -> str:
    """Kiwoom numerics are sign-prefixed strings like `-0000012345`."""
    if value is None or value == "":
        return "-"
    s = str(value).strip()
    sign = "-" if s.startswith("-") else ""
    digits = s.lstrip("+-").lstrip("0") or "0"
    if "." in digits:
        whole, _, frac = digits.partition(".")
        try:
            return f"{sign}{int(whole or 0):,}.{frac}"
        except ValueError:
            return s
    if not digits.isdigit():
        return s
    return f"{sign}{int(digits):,}"


def _labels(store: SpecStore | None, api_id: str) -> dict[str, str]:
    """element -> Korean label, from the spec's response table."""
    if store is None:
        return {}
    try:
        spec = store.get(api_id)
    except KeyError:
        return {}
    return {f.element: f.korean_name for f in spec.body_fields("response")}


def _rows(payload: dict) -> list[dict]:
    """Find the repeated-record list in a response body."""
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _scalars(payload: dict, labels: dict[str, str], limit: int) -> list[str]:
    out = []
    for key, value in payload.items():
        if key.startswith("return_") or isinstance(value, list | dict):
            continue
        if value in ("", None):
            continue
        out.append(f"{labels.get(key, key)}: {_num(value)}")
        if len(out) >= limit:
            break
    return out


def format_status(
    snapshot: Snapshot,
    store: SpecStore,
    endpoints,
    *,
    max_positions: int = 12,
    max_fields: int = 6,
) -> str:
    """Build the `/status` message from a broker snapshot."""
    taken = snapshot.taken_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"*{snapshot.market} account* — {taken}"]

    cash_labels = _labels(store, endpoints.cash.api_id) if endpoints else {}
    if cash := _scalars(snapshot.cash, cash_labels, max_fields):
        lines.append("\n*예수금 / Cash*")
        lines += [f"  {c}" for c in cash]

    pos_labels = _labels(store, endpoints.positions.api_id) if endpoints else {}
    rows = _rows(snapshot.positions)
    lines.append(f"\n*보유종목 / Positions* ({len(rows)})")
    if not rows:
        lines.append("  none")
    for row in rows[:max_positions]:
        name = row.get("stk_nm") or row.get("stk_cd") or "?"
        qty = _num(row.get("rmnd_qty") or row.get("hldg_qty") or row.get("qty"))
        pl = row.get("evltv_prft") or row.get("evlt_prft") or row.get("prft_rt")
        lines.append(f"  {name}  x{qty}" + (f"  P/L {_num(pl)}" if pl not in (None, "") else ""))
    if len(rows) > max_positions:
        lines.append(f"  … and {len(rows) - max_positions} more")

    if summary := _scalars(snapshot.positions, pos_labels, max_fields):
        lines.append("\n*평가 / Evaluation*")
        lines += [f"  {s}" for s in summary]

    open_rows = _rows(snapshot.open_orders)
    lines.append(f"\n*미체결 / Open orders* ({len(open_rows)})")
    for row in open_rows[:max_positions]:
        name = row.get("stk_nm") or row.get("stk_cd") or "?"
        qty = _num(row.get("ord_qty"))
        remaining = _num(row.get("oso_qty") or row.get("rmnd_qty"))
        lines.append(f"  {name}  ord {qty} / left {remaining}")
    if not open_rows:
        lines.append("  none")

    return "\n".join(lines)


class StatusReporter:
    """Produces status text from a live broker read."""

    def __init__(self, state: AccountState, store: SpecStore | None = None):
        self.state = state
        # Binance has no spec workbook, so labels simply fall back to raw field
        # names there. A missing store must not be fatal to reporting status.
        self.store = store or getattr(state.client, "store", None)
        self.endpoints = getattr(state, "endpoints", None)

    def report(self) -> str:
        # reconcile(), not current(): a status request is a question about the
        # broker's records right now, so never answer it from a cached snapshot.
        snapshot = self.state.reconcile()
        return format_status(snapshot, self.store, self.endpoints)

    def safe_report(self) -> str:
        """Never raise into the chat loop -- report the failure instead."""
        try:
            return self.report()
        except Exception as exc:
            log.exception("status report failed")
            stamp = dt.datetime.now(dt.UTC).astimezone().strftime("%H:%M:%S")
            return f"⚠️ status unavailable ({stamp})\n`{type(exc).__name__}: {exc}`"
