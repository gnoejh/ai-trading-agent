"""Overlapping observations: the defect that inverted the mainnet gate.

The live arms re-measure a symbol that is still in flight. The model named
HEMIUSDT on 117 of 615 Binance decisions; each opened its own observation with
its own 72h horizon, so one price path was scored 117 times and counted as 117
independent trials. The shadow, drawing uniformly from a 25-name menu, spread
over twice as many symbols -- so the inflation was ASYMMETRIC, and the arm that
concentrates was penalised for concentrating.

Measured on the live corpus 2026-09-16, that one bug reversed the gate's only
blocking criterion:

    raw     n=615  model -5.28% vs shadow -2.62%   edge -2.66%  (CI excluded 0)
    indep   n=68   model -2.16% vs shadow -3.13%   edge +0.97%

This is methodology trap #2 from *Research findings*, which the repo had already
paid for once. Nothing pinned it. These do.
"""

from __future__ import annotations

import datetime as dt

from trading.agent.scorer import independent

HORIZON = dt.timedelta(hours=72)


def _obs(symbol: str, hours: int, source: str = "model") -> dict:
    return {
        "symbol": symbol,
        "source": source,
        "ts": (dt.datetime(2026, 9, 1, tzinfo=dt.UTC) + dt.timedelta(hours=hours)).isoformat(),
        "forward_return_pct": -5.0,
    }


def test_a_symbol_re_measured_mid_flight_counts_once():
    """Three sightings of one symbol inside one horizon are ONE observation."""
    rows = [_obs("HEMIUSDT", 0), _obs("HEMIUSDT", 1), _obs("HEMIUSDT", 2)]
    assert len(independent(rows, HORIZON)) == 1


def test_the_same_symbol_past_the_horizon_is_a_new_trial():
    """Past 72h the price path no longer overlaps, so it is real new evidence."""
    rows = [_obs("HEMIUSDT", 0), _obs("HEMIUSDT", 73)]
    assert len(independent(rows, HORIZON)) == 2


def test_the_first_sighting_is_the_one_kept():
    """Keeping the LAST would let a late re-measure pick its own entry price."""
    rows = [_obs("X", 5), _obs("X", 1), _obs("X", 3)]
    kept = independent(rows, HORIZON)
    assert len(kept) == 1
    assert kept[0]["ts"] == _obs("X", 1)["ts"]


def test_different_symbols_never_suppress_each_other():
    rows = [_obs("A", 0), _obs("B", 0), _obs("C", 1)]
    assert len(independent(rows, HORIZON)) == 3


def test_an_unusable_timestamp_is_dropped_not_waved_through():
    """Treating an unparseable stamp as independent is the failure that matters.

    It would let exactly the rows the filter cannot reason about inflate n.
    """
    rows = [_obs("A", 0), {"symbol": "A", "ts": "not-a-date", "forward_return_pct": 1.0}]
    assert len(independent(rows, HORIZON)) == 1


def test_concentration_is_penalised_without_the_filter():
    """The asymmetry itself, stated as a test.

    A concentrated arm and a spread arm holding the SAME per-symbol outcomes
    must compare equal. Counting raw rows makes the concentrated one look worse
    purely because it repeated itself -- which is what the gate was reading.
    """
    concentrated = [_obs("LOSER", h) for h in range(10)] + [_obs("WINNER", 0)]
    for row in concentrated:
        row["forward_return_pct"] = -10.0 if row["symbol"] == "LOSER" else +10.0
    spread = [_obs("LOSER", 0), _obs("WINNER", 0)]
    for row in spread:
        row["forward_return_pct"] = -10.0 if row["symbol"] == "LOSER" else +10.0

    raw = sum(r["forward_return_pct"] for r in concentrated) / len(concentrated)
    assert raw < -6  # the concentrated arm looks terrible on raw rows

    kept = independent(concentrated, HORIZON)
    deduped = sum(r["forward_return_pct"] for r in kept) / len(kept)
    fair = sum(r["forward_return_pct"] for r in spread) / len(spread)
    assert deduped == fair == 0.0


# -- hurdle relabelling -------------------------------------------------------


def _scorer(crypto_hurdle_pct: float):
    """A scorer wired to one known hurdle. No network, no ledger file."""
    from types import SimpleNamespace

    from trading.agent.scorer import ExperienceScorer
    from trading.config import load_config

    ledger = SimpleNamespace(breakeven_move_pct=lambda market: crypto_hurdle_pct / 100)
    return ExperienceScorer(
        client=SimpleNamespace(), screen=SimpleNamespace(), ledger=ledger, cfg=load_config()
    )


def test_a_stale_miss_becomes_a_clear_when_the_hurdle_falls():
    """The whole point: +0.4% missed a 0.500% bar and clears a 0.300% one.

    `cleared_hurdle` is written at resolve time, so it is the fit target, the
    bucket clear rate and part of what the prompt reads back -- a stale label
    teaches the loop against a bar that no longer exists.
    """
    rows = [
        {"book": "CRYPTO", "forward_return_pct": 0.4, "cleared_hurdle": False, "hurdle_pct": 0.5}
    ]
    assert _scorer(0.30)._relabel(rows) == 1
    assert rows[0]["cleared_hurdle"] is True
    assert rows[0]["hurdle_pct"] == 0.3


def test_the_original_verdict_is_kept_for_audit():
    """Overwriting the record without trace would make the change unprovable."""
    rows = [
        {"book": "CRYPTO", "forward_return_pct": 0.4, "cleared_hurdle": False, "hurdle_pct": 0.5}
    ]
    _scorer(0.30)._relabel(rows)
    assert rows[0]["hurdle_pct_at_resolve"] == 0.5


def test_a_row_already_correct_is_not_counted_as_relabelled():
    """The counter must report real flips, or it cannot be read as a signal."""
    rows = [
        {"book": "CRYPTO", "forward_return_pct": 5.0, "cleared_hurdle": True, "hurdle_pct": 0.5}
    ]
    assert _scorer(0.30)._relabel(rows) == 0
    assert rows[0]["cleared_hurdle"] is True


def test_the_comparison_is_strict_at_the_boundary():
    """A return EXACTLY at the hurdle has not cleared it -- it has broken even.

    Calling that a win would let the clear rate count trades that paid for
    themselves and nothing else.
    """
    rows = [{"book": "CRYPTO", "forward_return_pct": 0.30, "cleared_hurdle": True}]
    _scorer(0.30)._relabel(rows)
    assert rows[0]["cleared_hurdle"] is False


def test_an_unresolved_row_is_left_alone():
    """No forward return means no verdict to reach -- not a silent False."""
    rows = [{"book": "CRYPTO", "forward_return_pct": None, "cleared_hurdle": None}]
    assert _scorer(0.30)._relabel(rows) == 0
    assert rows[0]["cleared_hurdle"] is None
