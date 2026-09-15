"""The screen replay: menu rules applied to historical cross-sections.

Its value rests on two things being exactly right. The OLD rule must be the
live `BinanceScreen.candidates()` of its day, or the replay measures a screen
nobody ran; and a cross-section's excess must be the mean over the menu minus
the benchmark drawn from the SAME rows, or the comparison is not paired. A
replay that gets either wrong would produce a confident prior about nothing.
"""

from __future__ import annotations

import json

import pytest

from trading.agent.screen_replay import _sample_menu, _union_menu, load_cross_sections, replay


def _row(symbol, vol, chg, flow, fwd=0.0):
    return {
        "symbol": symbol,
        "book": "CRYPTO",
        "price": 1.0,
        "quote_volume": vol,
        "change_pct": chg,
        "taker_share": flow,
        "forward_return_pct": fwd,
    }


def test_the_old_rule_prefers_names_in_both_heads():
    """A name in the volume head AND the flow head outranks one in either alone."""
    rows = [
        _row("BOTH", vol=100, chg=1.0, flow=0.9),
        _row("VOLUME_ONLY", vol=90, chg=1.0, flow=0.1),
        _row("FLOW_ONLY", vol=1, chg=1.0, flow=0.8),
    ] + [_row(f"F{i}", vol=2 + i, chg=1.0, flow=0.2) for i in range(20)]
    menu = _union_menu(rows, slots=1, lo=0.0, hi=0.15)
    assert [e["symbol"] for e in menu] == ["BOTH"]


def test_the_old_rule_applies_the_change_band_and_needs_flow():
    rows = [
        _row("TOO_HOT", vol=100, chg=20.0, flow=0.9),  # |chg| 20% > 15%
        _row("NO_FLOW", vol=90, chg=1.0, flow=None),
        _row("OK", vol=80, chg=1.0, flow=0.5),
    ]
    assert [e["symbol"] for e in _union_menu(rows, slots=5, lo=0.0, hi=0.15)] == ["OK"]


def test_the_0830_band_is_the_hot_band():
    rows = [_row("HOT", vol=100, chg=20.0, flow=0.5), _row("QUIET", vol=90, chg=1.0, flow=0.5)]
    assert [e["symbol"] for e in _union_menu(rows, slots=5, lo=0.15, hi=0.60)] == ["HOT"]


def test_sample_spans_the_liquidity_range():
    rows = [_row(f"S{i}", vol=1000 - i, chg=0.0, flow=0.5) for i in range(100)]
    menu = _sample_menu(rows, slots=10)
    vols = [e["quote_volume"] for e in menu]
    assert len(menu) == 10 and max(vols) == 1000 and min(vols) < 920


def test_replay_pairs_each_menu_against_its_own_cross_section(tmp_path):
    """Two cross-sections; the benchmark differs between them, so a menu's excess
    must be computed against ITS section's BTC, not a pooled one."""
    from trading.config import load_config

    cfg = load_config()
    cfg.score.observations = str(tmp_path / "obs.jsonl")
    cfg.score.screen_replay_min_group = 3
    cfg.agent.screen.book_slots = {"CRYPTO": 2}
    lines = []

    def section(ts, btc_fwd, others):
        for sym, vol, fwd in [("BTCUSDT", 1e9, btc_fwd)] + others:
            oid = f"backtest:{sym}:{ts}"
            lines.append(
                json.dumps(
                    {
                        "kind": "open",
                        "id": oid,
                        "source": "backtest",
                        "symbol": sym,
                        "ts": ts,
                        "book": "CRYPTO",
                        "price": 1.0,
                        "quote_volume": vol,
                        "change_pct": 0.0,
                        "taker_share": 0.5,
                    }
                )
            )
            lines.append(json.dumps({"kind": "resolve", "id": oid, "forward_return_pct": fwd}))

    section("2026-01-01T00:00:00+00:00", btc_fwd=1.0, others=[("A", 5e8, 3.0), ("B", 1e6, 5.0)])
    section("2026-01-04T00:00:00+00:00", btc_fwd=-2.0, others=[("A", 5e8, 0.0), ("B", 1e6, -2.0)])
    (tmp_path / "obs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    groups = load_cross_sections(tmp_path / "obs.jsonl")
    assert len(groups) == 2

    result = replay(cfg)
    pool = result["books"]["CRYPTO"]["pool"]
    # section 1: pool mean (1+3+5)/3 = 3, excess +2 ; section 2: mean (-2+0-2)/3 = -1.33, excess +0.67
    assert pool["n_cross_sections"] == 2
    assert pool["avg_excess_pct"] == pytest.approx((2.0 + 0.6667) / 2, abs=0.01)
    sample = result["books"]["CRYPTO"]["sample"]
    assert "menu_minus_pool_pct" in sample and sample["n_cross_sections"] == 2


def test_a_section_without_its_benchmark_is_not_scored(tmp_path):
    """No BTC in the section means no paired excess -- skip, never assume zero."""
    from trading.config import load_config

    cfg = load_config()
    cfg.score.observations = str(tmp_path / "obs.jsonl")
    cfg.score.screen_replay_min_group = 1
    ts = "2026-01-01T00:00:00+00:00"
    rows = [
        json.dumps(
            {
                "kind": "open",
                "id": f"backtest:A:{ts}",
                "source": "backtest",
                "symbol": "A",
                "ts": ts,
                "book": "CRYPTO",
                "price": 1.0,
                "quote_volume": 1e6,
                "change_pct": 0.0,
                "taker_share": 0.5,
            }
        ),
        json.dumps({"kind": "resolve", "id": f"backtest:A:{ts}", "forward_return_pct": 4.0}),
    ]
    (tmp_path / "obs.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert replay(cfg)["cross_sections_used"] == 0
