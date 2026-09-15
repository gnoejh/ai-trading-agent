"""The selector arms replayed over backtest cross-sections.

Each arm picks ONE name from the section's `sample` menu and is paired against
a random draw from that same menu -- its mean -- which is the exact analogue
of the live leaderboard's model-vs-shadow pairing. Getting the pairing wrong
(pick vs the pool mean, or vs a different section) would produce a confident
six-month prior about nothing, so the arithmetic is pinned on a hand-computable
section.
"""

from __future__ import annotations

import json

import pytest

from trading.agent.screen_replay import replay

NL = chr(10)


def _write(path, sections):
    lines = []
    for ts, rows in sections:
        for sym, vol, chg, flow, fwd in rows:
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
                        "change_pct": chg,
                        "taker_share": flow,
                    }
                )
            )
            lines.append(json.dumps({"kind": "resolve", "id": oid, "forward_return_pct": fwd}))
    path.write_text(NL.join(lines) + NL, encoding="utf-8")


def test_each_arm_is_paired_against_its_own_menu_mean(tmp_path):
    """One section, menu = the whole pool (slots >= names), so the arithmetic is
    visible: flow_top picks FLOW (+4), shadow mean = (1+4+0+2)/4 = 1.75 -> +2.25."""
    from trading.config import load_config

    cfg = load_config()
    cfg.score.observations = str(tmp_path / "obs.jsonl")
    cfg.score.screen_replay_min_group = 1
    cfg.agent.screen.book_slots = {"CRYPTO": 10}
    ts = "2026-01-01T00:00:00+00:00"
    _write(
        tmp_path / "obs.jsonl",
        [
            (
                ts,
                [
                    #  sym        vol   chg   flow  fwd
                    ("BTCUSDT", 1e9, 0.0, 0.50, 1.0),
                    ("FLOW", 1e6, 0.0, 0.90, 4.0),
                    ("HOT", 2e6, 9.0, 0.40, 0.0),
                    ("COLD", 3e6, -9.0, 0.30, 2.0),
                ],
            )
        ],
    )
    arms = {r["selector"]: r for r in replay(cfg)["arms"]["CRYPTO"]}
    assert arms["flow_top"]["edge_vs_shadow_pct"] == pytest.approx(4.0 - 1.75, abs=1e-3)
    assert arms["volume_top"]["edge_vs_shadow_pct"] == pytest.approx(1.0 - 1.75, abs=1e-3)
    assert arms["change_high"]["edge_vs_shadow_pct"] == pytest.approx(0.0 - 1.75, abs=1e-3)
    assert arms["change_low"]["edge_vs_shadow_pct"] == pytest.approx(2.0 - 1.75, abs=1e-3)
    # `p_clear` is not a backtest feature, so the prior arm yields nothing here
    # -- and must not be reported as if it had been measured.
    assert "prior_top" not in arms


def test_arms_are_sorted_by_the_ci_lower_bound_like_the_live_board(tmp_path):
    from trading.config import load_config

    cfg = load_config()
    cfg.score.observations = str(tmp_path / "obs.jsonl")
    cfg.score.screen_replay_min_group = 1
    cfg.agent.screen.book_slots = {"CRYPTO": 10}
    sections = []
    for day in range(1, 6):
        ts = f"2026-01-{day:02d}T00:00:00+00:00"
        sections.append(
            (
                ts,
                [
                    ("BTCUSDT", 1e9, 0.0, 0.50, 0.0),
                    ("FLOW", 1e6, 0.0, 0.90, 3.0 + day * 0.1),  # consistently good
                    ("HOT", 2e6, 9.0, 0.40, -3.0),  # consistently bad
                ],
            )
        )
    _write(tmp_path / "obs.jsonl", sections)
    rows = replay(cfg)["arms"]["CRYPTO"]
    lows = [r["ci_low"] for r in rows if r["ci_low"] is not None]
    assert lows == sorted(lows, reverse=True)
    assert rows[0]["selector"] == "flow_top" and rows[-1]["selector"] == "change_high"
