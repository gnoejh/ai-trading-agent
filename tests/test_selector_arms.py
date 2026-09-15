"""Selector arms: the LLM is no longer the only selector on trial.

Each arm is a pure function of a decision's journalled MENU, opened at scoring
time as its own source and paired against the same shadow on the same menus.
Two properties matter and are pinned here. DETERMINISM: a scorer re-run must
reopen the same observation id, not a fresh one, or the corpus doubles on every
pass -- so ties break on symbol, never on list order. RETROACTIVITY: the arms
read the menu already on every decision record, which is why they cost
nothing and why the first leaderboard covered the whole epoch the day they
were added.
"""

from __future__ import annotations

import pytest

from trading.agent.scorer import ARM_PREFIX, SELECTORS, _pick_by

MENU = [
    {"symbol": "AAAUSDT", "taker_buy_share": 0.40, "p_clear": 0.60, "quote_volume": 9e6, "change_pct": -5.0},
    {"symbol": "BBBUSDT", "taker_buy_share": 0.70, "p_clear": 0.45, "quote_volume": 1e6, "change_pct": 12.0},
    {"symbol": "CCCUSDT", "taker_buy_share": 0.55, "p_clear": 0.52, "quote_volume": 5e6, "change_pct": 0.5},
]


@pytest.mark.parametrize(
    "arm,expected",
    [
        ("flow_top", "BBBUSDT"),
        ("prior_top", "AAAUSDT"),
        ("volume_top", "AAAUSDT"),
        ("change_low", "AAAUSDT"),
        ("change_high", "BBBUSDT"),
    ],
)
def test_each_arm_picks_the_extreme_of_its_own_feature(arm, expected):
    assert SELECTORS[arm](MENU) == expected


def test_a_tie_breaks_on_symbol_not_on_menu_order():
    """Two scorer runs over a shuffled menu must open the SAME observation id."""
    tied = [
        {"symbol": "ZZZUSDT", "taker_buy_share": 0.6},
        {"symbol": "AAAUSDT", "taker_buy_share": 0.6},
    ]
    assert _pick_by(tied, "taker_buy_share", largest=True) == "ZZZUSDT"
    assert _pick_by(list(reversed(tied)), "taker_buy_share", largest=True) == "ZZZUSDT"


def test_a_menu_without_the_feature_yields_no_pick():
    """No flow data means no flow pick -- never a silent first-row default."""
    assert _pick_by([{"symbol": "X"}, {"symbol": "Y"}], "taker_buy_share", largest=True) is None


def test_an_unparseable_feature_is_skipped_not_fatal():
    menu = [{"symbol": "X", "p_clear": "n/a"}, {"symbol": "Y", "p_clear": 0.5}]
    assert _pick_by(menu, "p_clear", largest=True) == "Y"


def test_every_configured_arm_exists():
    """A typo in config must fail loudly here, not log-and-skip in production."""
    from trading.config import load_config

    for name in load_config().score.arms:
        assert name in SELECTORS, name


def test_arm_sources_are_namespaced_away_from_the_live_sources():
    """`arm_` keeps them out of the fit's source list and the model's own bucket."""
    from trading.agent.scorer import LIVE_SOURCES

    for name in SELECTORS:
        assert f"{ARM_PREFIX}{name}" not in LIVE_SOURCES
