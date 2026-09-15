"""Case-based retrieval: the mechanics that make a neighbour a fair witness.

A case with a missing feature is not comparable (None, never filled); a query
must not see the future (validation enforces `before`); distances are on
standardised features so turnover does not swamp everything; and the live
`annotate` only decorates candidates that carry the full vector.
"""

from __future__ import annotations

from trading.agent.similar import SimilarIndex, annotate, vector

KEYS = ["change_pct", "log_turnover", "taker_share"]


def _row(ts, chg, qv, flow, fwd, hit):
    return {
        "ts": ts,
        "change_pct": chg,
        "quote_volume": qv,
        "taker_share": flow,
        "forward_return_pct": fwd,
        "cleared_hurdle": hit,
        "excess_return_pct": fwd - 1.0,
    }


def test_a_missing_feature_means_no_vector():
    assert vector({"change_pct": 1.0, "quote_volume": 0, "taker_share": 0.5}, KEYS) is None
    assert vector({"change_pct": 1.0, "quote_volume": 10, "taker_share": None}, KEYS) is None
    # `taker_buy_share` (the live candidate's name) stands in for taker_share.
    assert vector({"change_pct": 1.0, "quote_volume": 10, "taker_buy_share": 0.5}, KEYS) is not None


def test_neighbours_are_the_closest_cases_and_report_their_outcomes():
    rows = [
        _row("2026-01-01T00:00:00+00:00", 1.0, 1e6, 0.50, +3.0, True),
        _row("2026-01-02T00:00:00+00:00", 1.1, 1e6, 0.51, +2.0, True),
        _row("2026-01-03T00:00:00+00:00", 1.2, 1e6, 0.49, +4.0, True),
        _row("2026-01-04T00:00:00+00:00", 50.0, 1e9, 0.90, -9.0, False),
        _row("2026-01-05T00:00:00+00:00", -40.0, 1e3, 0.10, -8.0, False),
    ]
    idx = SimilarIndex(KEYS, rows)
    q = idx.query(vector({"change_pct": 1.05, "quote_volume": 1e6, "taker_share": 0.5}, KEYS), 3)
    assert q["hit_rate"] == 1.0 and q["avg_return_pct"] == 3.0


def test_a_query_never_sees_the_future():
    rows = [
        _row("2026-01-01T00:00:00+00:00", 1.0, 1e6, 0.5, +1.0, True),
        _row("2026-01-02T00:00:00+00:00", 1.0, 1e6, 0.5, +1.0, True),
        _row("2026-01-09T00:00:00+00:00", 1.0, 1e6, 0.5, -9.0, False),  # after the query
    ]
    idx = SimilarIndex(KEYS, rows)
    v = vector({"change_pct": 1.0, "quote_volume": 1e6, "taker_share": 0.5}, KEYS)
    q = idx.query(v, 2, before="2026-01-05T00:00:00+00:00")
    assert q["hit_rate"] == 1.0, "the -9% case resolved after the query and must be invisible"
    assert idx.query(v, 3, before="2026-01-05T00:00:00+00:00") is None, "fewer than k earlier cases"


def test_distances_are_standardised_so_turnover_cannot_swamp():
    # Two cases identical except turnover 1e6 vs 1e7 (log 13.8 vs 16.1), and a
    # third with the same turnover as the query but far away in flow.
    rows = [
        _row("2026-01-01T00:00:00+00:00", 0.0, 1e6, 0.50, +1.0, True),
        _row("2026-01-02T00:00:00+00:00", 0.0, 1e7, 0.50, +2.0, True),
        _row("2026-01-03T00:00:00+00:00", 0.0, 1e6, 0.95, -5.0, False),
        _row("2026-01-04T00:00:00+00:00", 9.0, 1e6, 0.05, -5.0, False),
    ]
    idx = SimilarIndex(KEYS, rows)
    q = idx.query(vector({"change_pct": 0.0, "quote_volume": 3e6, "taker_share": 0.5}, KEYS), 2)
    assert q["hit_rate"] == 1.0, "the two flow-0.5 cases are nearest once features are standardised"


def test_annotate_decorates_only_full_vectors():
    rows = [_row(f"2026-01-0{i}T00:00:00+00:00", 1.0, 1e6, 0.5, 1.0, True) for i in range(1, 6)]
    idx = SimilarIndex(KEYS, rows)
    cands = [
        {"symbol": "OK", "change_pct": 1.0, "quote_volume": 1e6, "taker_buy_share": 0.5},
        {"symbol": "HOLE", "change_pct": 1.0, "quote_volume": 1e6},
    ]
    assert annotate(cands, idx, 3) == 1
    assert "similar_setups" in cands[0] and "similar_setups" not in cands[1]
    assert cands[0]["similar_setups"]["n"] == 3


def test_round_trips_through_json():
    rows = [
        _row(f"2026-01-0{i}T00:00:00+00:00", float(i), 1e6, 0.5, 1.0, True) for i in range(1, 6)
    ]
    idx = SimilarIndex(KEYS, rows)
    back = SimilarIndex.from_json(idx.to_json())
    v = vector({"change_pct": 2.0, "quote_volume": 1e6, "taker_share": 0.5}, KEYS)
    assert back.query(v, 2) == idx.query(v, 2)
