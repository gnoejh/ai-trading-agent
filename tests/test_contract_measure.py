"""The gate grades an event the system does not trade.

`forward_return_pct` is a 72h buy-and-hold. Nothing here is held that way: the
trail, the stop, the target and the time stop close every real position, and
the 2026-09-16 audit found the exit CONTRACT is where the profit comes from. So
the gate's only blocking criterion was measuring one strategy while the account
ran another -- *Research findings* trap #5 ("grade what you asked"), one level
up, inside the gate itself.

Measured on the live corpus 2026-09-19, same arms, same de-overlap, same bar:

    raw hold   n=174  model -1.01% vs random -1.11%  edge +0.10%  CI -2.08..+2.19
    contract   n=174  model -0.09% vs random -0.26%  edge +0.16%  CI -1.13..+1.44
    (BINANCE)  raw -0.19%  ->  contract +0.34%

The corroboration that the contract is the right instrument is that it
reproduces the money: Binance model picks read +0.64%/trip gross under it,
against a realised sleeve of +0.36%/trip net on a ~0.30% round trip. The raw
measurement read -1.11% for those same picks.

Two things must stay true, and they are what these tests pin. (1) It is not a
softer bar -- the interval still straddles zero, so this does NOT open the
gate, and `evaluate()` keeps grading the criterion on the raw hold until the
owner moves it. (2) A pair measured one way on one side and another way on the
other is not a comparison: provenance must be homogeneous within a pair, or the
number is one nothing produced.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from trading.accounting.costs import CostLedger
from trading.agent.promotion import evaluate
from trading.agent.scorer import (
    CONTRACT_APPROX,
    CONTRACT_EXACT,
    ExperienceScorer,
    contract_return_pct,
)
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.exits.state = str(tmp_path / "exits.json")
    return c


def _scorer(cfg):
    sc = ExperienceScorer.__new__(ExperienceScorer)
    sc.cfg = cfg
    sc.scfg = cfg.score
    sc.ledger = CostLedger(cfg)
    sc._bench_cache = {}
    sc._sources = {}
    return sc


# -- what the contract return IS -------------------------------------------


def test_a_stored_replay_wins_and_is_labelled_exact():
    """A row resolved since the replay shipped is used verbatim, trail included."""
    row = {
        "contract_return_pct": 1.23,
        "outcome": "stop",
        "stop_pct": 8.0,
        "forward_return_pct": -30.0,
    }
    assert contract_return_pct(row) == (1.23, CONTRACT_EXACT)


def test_a_stop_is_the_stop_not_the_horizon_close():
    """The whole point: a stopped position never saw the horizon's return."""
    row = {"outcome": "stop", "stop_pct": 8.0, "target_pct": 17.8, "forward_return_pct": -42.0}
    value, how = contract_return_pct(row)
    assert value == -8.0
    assert how == CONTRACT_APPROX


def test_a_target_is_the_target_not_the_giveback():
    row = {"outcome": "target", "stop_pct": 8.0, "target_pct": 17.8, "forward_return_pct": 2.0}
    assert contract_return_pct(row) == (17.8, CONTRACT_APPROX)


def test_a_time_exit_is_the_horizon_close():
    row = {"outcome": "time", "stop_pct": 8.0, "target_pct": 17.8, "forward_return_pct": 3.5}
    assert contract_return_pct(row) == (3.5, CONTRACT_APPROX)


def test_an_unresolved_row_measures_nothing():
    """Absent, never 0.0 -- a zero would be a real observation of no move."""
    assert contract_return_pct({"outcome": None, "forward_return_pct": None}) is None


def test_a_failed_replay_falls_back_instead_of_reading_as_zero():
    """`contract_return_pct: None` is exactly what a failed replay writes."""
    row = {
        "contract_return_pct": None,
        "outcome": "stop",
        "stop_pct": 8.0,
        "forward_return_pct": -42.0,
    }
    assert contract_return_pct(row) == (-8.0, CONTRACT_APPROX)


# -- the pairing -----------------------------------------------------------


def _pair_rows(n, model_outcome, shadow_outcome):
    """Distinct symbols, >72h apart, so de-overlap keeps every pair."""
    base = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    model, shadow = [], []
    for i in range(n):
        ts = (base + dt.timedelta(hours=100 * i)).isoformat()
        common = {
            "venue": "BINANCE",
            "ts": ts,
            "decision_ts": ts,
            "forward_return_pct": -20.0,
            "stop_pct": 8.0,
            "target_pct": 17.8,
        }
        model.append({"source": "model", "symbol": f"M{i}", "outcome": model_outcome, **common})
        shadow.append({"source": "shadow", "symbol": f"S{i}", "outcome": shadow_outcome, **common})
    return model, shadow


def test_the_contract_separates_arms_the_raw_hold_cannot(cfg):
    """Identical horizon returns, different PATHS -- the contract sees it.

    Both arms end the horizon at -20%. The model's positions were stopped at
    -8% on the way; the shadow's touched the target first. The raw comparison
    calls these identical. Under the contract they are 25.8pp apart. That gap
    is the measurement error the gate has been carrying.
    """
    sc = _scorer(cfg)
    model, shadow = _pair_rows(12, "stop", "target")

    raw = sc._pairs(model, shadow)
    assert raw["n"] == 12
    assert raw["mean_diff_pct"] == 0.0, "the raw hold cannot see the path"

    contract = sc._pairs(model, shadow, value=contract_return_pct)
    assert contract["model_avg_pct"] == -8.0
    assert contract["shadow_avg_pct"] == 17.8
    assert contract["mean_diff_pct"] == pytest.approx(-25.8)


def test_a_pair_never_mixes_provenance(cfg):
    """One side replayed and the other reconstructed is not a comparison."""
    sc = _scorer(cfg)
    model, shadow = _pair_rows(6, "stop", "stop")
    for row in model:  # only the model side carries a stored replay
        row["contract_return_pct"] = -3.0

    out = sc._pairs(model, shadow, value=contract_return_pct)
    assert out["n"] == 0
    assert out["mixed_provenance_dropped"] == 6


def test_homogeneous_pairs_report_their_provenance(cfg):
    sc = _scorer(cfg)
    model, shadow = _pair_rows(6, "stop", "stop")
    out = sc._pairs(model, shadow, value=contract_return_pct)
    assert out["measured"] == {CONTRACT_APPROX: 6}
    assert out["mixed_provenance_dropped"] == 0


def test_the_contract_inherits_the_de_overlap_rule(cfg):
    """Trap #2 must not be re-opened by measuring a different event."""
    sc = _scorer(cfg)
    base = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    model, shadow = [], []
    for i in range(5):  # ONE symbol, an hour apart: one observation, not five
        ts = (base + dt.timedelta(hours=i)).isoformat()
        common = {
            "venue": "BINANCE",
            "ts": ts,
            "decision_ts": ts,
            "forward_return_pct": -20.0,
            "stop_pct": 8.0,
            "target_pct": 17.8,
        }
        model.append({"source": "model", "symbol": "HEMIUSDT", "outcome": "stop", **common})
        shadow.append({"source": "shadow", "symbol": f"S{i}", "outcome": "time", **common})

    out = sc._pairs(model, shadow, value=contract_return_pct)
    assert out["n"] == 1
    assert out["n_raw"] == 5


# -- it is a reading, NOT the bar ------------------------------------------


def test_the_contract_reading_cannot_open_the_gate(cfg, tmp_path):
    """The criterion stays on the raw hold until the OWNER moves it.

    A contract summary that clears the bar while the criterion does not must
    still leave the gate shut. Otherwise this change would be a way of lowering
    a bar to manufacture a promotion, which is the one thing the record forbids.
    """
    store = {
        "model_vs_shadow": {
            "n": 200,
            "mean_diff_pct": -1.0,
            "ci_low": -3.0,
            "ci_high": 1.0,
            "model_avg_pct": -1.0,
            "shadow_avg_pct": 0.0,
            "model_wins": 80,
        },
        "model_vs_shadow_contract": {
            "n": 200,
            "mean_diff_pct": 5.0,
            "ci_low": 2.0,
            "ci_high": 8.0,
            "model_avg_pct": 5.0,
            "shadow_avg_pct": 0.0,
            "model_wins": 150,
            "measured": {CONTRACT_APPROX: 200},
        },
    }
    path = tmp_path / "experience.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    cfg.score.experience = str(path)

    result = evaluate(cfg)
    beats = [c for c in result["checks"] if c["name"].startswith("model beats its shadow")]
    assert beats, "the gate must still carry the model-beats-shadow criterion"
    assert not beats[0]["ok"], "a green contract reading must not satisfy the raw criterion"
    assert not result["ready"]


# -- the seam: the replay must actually run at resolve time ----------------


class _StubSource:
    """A price record built from a hand-written path."""

    def __init__(self, window):
        self._window = window

    def window(self, symbol, start, end):
        return self._window


def _window(entry, path, opened, hours=72):
    """Hourly bars whose high/low bracket each close by 0.1%."""
    from trading.agent.prices import Bar, Window

    start_ms = int(opened.timestamp() * 1000)
    bars = [
        Bar(
            t=start_ms + i * 3_600_000,
            open=entry if i == 0 else path[i - 1],
            high=max(path[i], entry if i == 0 else path[i - 1]) * 1.001,
            low=min(path[i], entry if i == 0 else path[i - 1]) * 0.999,
            close=path[i],
        )
        for i in range(len(path))
    ]
    return Window(bars=bars, complete=True, interval="1h")


def test_resolve_stores_the_contracts_return_and_its_exit_reason(cfg):
    """The seam. `simulate` is the exit grid's replay; the resolve path must
    actually call it, or this whole measurement is a docstring.

    A path that runs up and gives it all back: the raw hold ends flat, the
    contract leaves on the ratcheted stop with the run in hand. If these two
    numbers ever agree on this path, the replay is not running.
    """
    sc = _scorer(cfg)
    opened = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    entry = 100.0
    path = [102.0, 106.0, 110.0, 112.0, 104.0, 100.0, 100.0]
    sc._sources["BINANCE"] = _StubSource(_window(entry, path, opened))

    row = sc._resolve_one(
        {"id": "model:XUSDT:t", "symbol": "XUSDT", "price": entry, "book": "CRYPTO"},
        opened,
        dt.timedelta(minutes=4320),
        "BINANCE",
        opened + dt.timedelta(days=5),
        dt.timedelta(hours=1),
    )

    assert row is not None
    assert row["forward_return_pct"] == pytest.approx(0.0, abs=0.01), "the hold ends flat"
    assert row["contract_return_pct"] is not None
    assert row["contract_exit"] == "trail", "a stop that ratcheted up is a TRAIL"
    assert row["contract_return_pct"] > 0, "the contract kept part of the run"
    value, how = contract_return_pct(row)
    assert how == CONTRACT_EXACT
    assert value == row["contract_return_pct"]


def test_a_replay_that_cannot_run_leaves_the_field_absent(cfg):
    """A failed replay must not write 0.0 -- that is a real observation of
    'no move', and it would quietly drag every arm toward zero."""

    class _Exploding:
        def window(self, symbol, start, end):
            return _window(100.0, [100.0] * 4, dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    sc = _scorer(cfg)
    opened = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    sc._sources["BINANCE"] = _Exploding()

    import trading.agent.scorer as scorer_mod

    original = scorer_mod.simulate
    scorer_mod.simulate = lambda *a, **k: (_ for _ in ()).throw(ValueError("no bars"))
    try:
        row = sc._resolve_one(
            {"id": "model:YUSDT:t", "symbol": "YUSDT", "price": 100.0, "book": "CRYPTO"},
            opened,
            dt.timedelta(minutes=4320),
            "BINANCE",
            opened + dt.timedelta(days=5),
            dt.timedelta(hours=1),
        )
    finally:
        scorer_mod.simulate = original

    assert row is not None
    assert row["contract_return_pct"] is None
    assert row["contract_exit"] is None
    # ...and aggregation still measures it, by the endpoint reconstruction.
    assert contract_return_pct(row)[1] == CONTRACT_APPROX
