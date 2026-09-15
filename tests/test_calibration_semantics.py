"""Calibration must grade the question the prompt actually asks.

The prompt defines `confidence` as the probability that the position ENDS IN
PROFIT AFTER COSTS -- by the trailing stop, the target, or the time stop.
Until 2026-09-16 the grader scored it against `cleared_target`: reaching the
full target before the stop, an event the same prompt says ends ~7% of
positions. So a perfectly calibrated model stating 0.50 was told, every cycle,
that it hit 7% -- "overconfident" in every band by construction -- and it did
what a well-behaved model does with that feedback: it declined. ~95% of cycles.
The self-censoring the calibration loop was built to CORRECT, it was causing.
"""

from __future__ import annotations

from types import SimpleNamespace

from trading.agent.scorer import ExperienceScorer, profitable


def _scorer():
    from trading.config import load_config

    cfg = load_config()
    cfg.score.min_bucket_n = 1
    ledger = SimpleNamespace(breakeven_move_pct=lambda market: 0.003)
    return ExperienceScorer(
        client=SimpleNamespace(), screen=SimpleNamespace(), ledger=ledger, cfg=cfg
    )


def test_a_target_hit_is_profit():
    assert profitable({"outcome": "target", "cleared_hurdle": True}) is True


def test_a_stop_is_a_loss_whatever_the_horizon_return_says():
    """Stopped out at -8% on day 1 and back above entry by day 3 is still a loss:
    the position was closed at the stop and never saw day 3."""
    assert profitable({"outcome": "stop", "cleared_hurdle": True}) is False


def test_a_time_exit_is_profit_only_past_costs():
    assert profitable({"outcome": "time", "cleared_hurdle": True}) is True
    assert profitable({"outcome": "time", "cleared_hurdle": False}) is False


def test_an_unresolved_outcome_is_not_graded():
    assert profitable({"outcome": None, "cleared_hurdle": True}) is None


def test_calibration_grades_profit_not_the_target():
    """The defect, as a test: a 0.50 stater who profits half the time is
    CALIBRATED -- and was being told it hit 0% because none reached +17%."""
    rows = [
        {
            "confidence": 0.5,
            "outcome": "time",
            "cleared_hurdle": True,
            "cleared_target": False,
            "forward_return_pct": 1.0,
            "venue": "BINANCE",
        },
        {
            "confidence": 0.5,
            "outcome": "stop",
            "cleared_hurdle": False,
            "cleared_target": False,
            "forward_return_pct": -8.0,
            "venue": "BINANCE",
        },
    ]
    [pooled, _venue] = _scorer()._calibration(rows)
    assert pooled["band"] == "0.45-0.55"
    assert pooled["hit_rate"] == 0.5, "half of them ended in profit"
    assert pooled["stated"] == 0.5
    # The old figure survives as its own field, so nothing is lost -- it is
    # simply no longer what "hit" means.
    assert pooled["target_rate"] == 0.0


def test_the_prompt_gives_confidence_one_definition():
    """Two definitions in one prompt was the other half of the defect."""
    from pathlib import Path

    from trading.agent import loop

    text = Path(loop.__file__).read_text(encoding="utf-8")
    assert "reaches the target before the stop" not in text
    assert "ends in profit after costs" in text
