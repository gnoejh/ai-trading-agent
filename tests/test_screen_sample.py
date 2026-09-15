"""The `sample` ranker: span the pool instead of skimming an edge of it.

Every ranker this repo has tried selects the EXTREME TAIL of something, and each
tail has now measured worse than the body it came from -- momentum had no edge
(2026-08-10), the fitted prior lost to a constant (2026-09-13), and flow does
not sort inside the menu at all (clear rate FALLS as flow rises: 0.36 / 0.29 /
0.23). The screen control makes the cost of that explicit: a RANDOM draw from
the menu returned -2.57% of excess on Binance against +1.36% for a random draw
from outside it, and -1.22% against +1.76% on KR.

`sample` therefore ranks by NOTHING on purpose. The tests that matter here are
not that it sorts correctly -- it must not sort at all -- but that it actually
CHANGES the menu. Switching the ranker with the Binance union scoring still in
place moved 6 of 25 names, because a name scores up for appearing in both the
volume head and the move ranking, so the liquidity head won regardless. That
would have shipped a no-op dressed as a fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading.brokers.binance.universe import BinanceScreen
from trading.config import load_config


def _pool(n: int) -> list[dict]:
    """A pool spanning four orders of magnitude of turnover, most-liquid first."""
    return [
        {
            "symbol": f"S{i:03d}",
            "book": "CRYPTO",
            "quote_volume": float(10**9 // (i + 1)),
            "change_pct": 1.0,
            "taker_buy_share": 0.9 - i / 1000,
        }
        for i in range(n)
    ]


@pytest.fixture
def screen(monkeypatch):
    cfg = load_config()
    cfg.agent.screen.rank_by = "sample"
    cfg.agent.screen.book_slots = {"CRYPTO": 10}
    cfg.agent.screen.use_flow = False
    s = BinanceScreen(SimpleNamespace(), SimpleNamespace(), cfg)
    monkeypatch.setattr(s, "tradable_pool", lambda order_size=0.0: _pool(400))
    return s


def test_the_menu_spans_the_pool_rather_than_its_head(screen):
    """The whole point: the least liquid name must come from deep in the pool.

    A head-skimming menu would put every name in the top 10 by turnover.
    """
    menu = screen.candidates()
    assert len(menu) == 10
    volumes = [e["quote_volume"] for e in menu]
    assert max(volumes) == 10**9  # still reaches the most liquid name
    # ...and still reaches far down: the thinnest pick is orders of magnitude
    # below the head, which a top-N ranker could never produce.
    assert min(volumes) < 10**9 / 100


def test_sample_is_not_the_liquidity_head_under_another_name(screen):
    """The failure mode with teeth: a "wider" menu that is still the top N.

    The old menu was the volume head intersected with the flow head. If the
    stride collapsed to the head, the change would read as shipped while the
    population stayed exactly as it was.
    """
    menu = [e["symbol"] for e in screen.candidates()]
    head = [e["symbol"] for e in _pool(400)[:10]]
    assert menu != head
    assert len(set(menu) & set(head)) <= 2


def test_sample_changes_the_menu_it_replaces(screen):
    """Pinning the no-op that nearly shipped.

    With the union scoring left in place, switching the ranker changed 6 of 25
    names. `sample` bypasses the union, so the menus must differ substantially.
    """
    sampled = {e["symbol"] for e in screen.candidates()}
    screen.cfg.agent.screen.rank_by = "flow"
    screen.cfg.agent.screen.use_flow = True
    flowed = {e["symbol"] for e in screen.candidates()}
    assert len(sampled & flowed) < len(sampled) / 2


def test_a_pool_smaller_than_the_slots_is_returned_whole(screen, monkeypatch):
    """The stride must never divide by zero or drop names when the pool is thin."""
    monkeypatch.setattr(screen, "tradable_pool", lambda order_size=0.0: _pool(3))
    assert len(screen.candidates()) == 3


def test_an_empty_pool_yields_an_empty_menu(screen, monkeypatch):
    monkeypatch.setattr(screen, "tradable_pool", lambda order_size=0.0: [])
    assert screen.candidates() == []


def test_sample_never_repeats_a_name(screen):
    """An off-by-one in the stride would duplicate names and shrink the menu."""
    menu = screen.candidates()
    assert len({e["symbol"] for e in menu}) == len(menu)


def test_a_book_with_zero_slots_is_skipped_not_divided_by(screen, monkeypatch):
    """BSTOCKS was paused with `book_slots: {BSTOCKS: 0}` on 2026-09-16.

    The stride is `len(pool) / min(len(pool), slots)`; with slots at zero that
    is a ZeroDivisionError on the first cycle -- the whole screen, both books,
    gone in one traceback. A zero-slot book must contribute nothing, quietly.
    """
    mixed = _pool(40)
    for e in mixed[20:]:
        e["book"] = "BSTOCKS"
    monkeypatch.setattr(screen, "tradable_pool", lambda order_size=0.0: mixed)
    screen.cfg.agent.screen.book_slots = {"CRYPTO": 10, "BSTOCKS": 0}
    menu = screen.candidates()
    assert len(menu) == 10
    assert {e["book"] for e in menu} == {"CRYPTO"}
