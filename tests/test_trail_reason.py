"""A ratcheted stop is a trail, and the live record must say so.

`ExitReason.TRAIL` existed and was never emitted: every stop hit was journalled
`stop_loss`, including RAYUSDT's on 2026-09-16, which closed ABOVE entry on a
stop that had ratcheted there. exit_eval had counted trails separately all
along (~70% of positions); the live journal disagreed with it by construction.
"""

from __future__ import annotations

import datetime as dt

from trading.config import load_config
from trading.risk.exits import ExitPlan, ExitPolicy, ExitReason


def _policy(tmp_path):
    cfg = load_config()
    cfg.exits.state = str(tmp_path / "exits.json")
    cfg.accounting.ledger = str(tmp_path / "ledger.jsonl")
    return ExitPolicy(cfg, market="BINANCE")


def test_a_fresh_stop_hit_is_a_stop(tmp_path):
    p = _policy(tmp_path)
    plan = p.plan_for("X", 100.0, 1.0)
    assert plan.initial_stop == plan.stop
    sig = p.evaluate(plan, plan.stop * 0.999, now=dt.datetime.now(dt.UTC))
    assert sig is not None and sig.reason is ExitReason.STOP


def test_a_ratcheted_stop_hit_is_a_trail(tmp_path):
    p = _policy(tmp_path)
    plan = p.plan_for("X", 100.0, 1.0)
    assert plan.tighten_stop(101.0)  # the trail earned a stop above entry
    sig = p.evaluate(plan, 100.9, now=dt.datetime.now(dt.UTC))
    assert sig is not None and sig.reason is ExitReason.TRAIL
    assert "raised from" in sig.detail if hasattr(sig, "detail") else True


def test_a_legacy_plan_without_the_field_reads_as_a_stop(tmp_path):
    """Plans persisted before the field existed load with initial_stop 0.0 and
    can only ever report a stop -- the conservative degradation, never a crash."""
    p = _policy(tmp_path)
    legacy = ExitPlan(
        symbol="OLD",
        entry_price=100.0,
        quantity=1.0,
        opened_at="2026-09-01T00:00:00+00:00",
        stop=103.0,  # already ratcheted before the field existed
        net_breakeven=100.3,
        target=117.0,
    )
    assert legacy.initial_stop == 0.0
    sig = p.evaluate(legacy, 102.0, now=dt.datetime.now(dt.UTC))
    assert sig is not None and sig.reason is ExitReason.STOP
