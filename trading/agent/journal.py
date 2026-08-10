"""Append-only decision record.

Every cycle writes what the agent saw, what it proposed, what the gate said and
what the broker replied. This is an audit trail, **not** state: nothing reads it
back to decide anything, because broker records are the single source of truth.
Without it you cannot answer "why did it buy that?" three days later, and you
cannot backtest the agent's own behaviour.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields) -> None:
        record = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "kind": kind,
            **fields,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            # Never let an audit-write failure abort a trading cycle.
            log.error("journal write failed: %s", exc)
