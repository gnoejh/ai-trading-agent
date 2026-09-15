"""The slippage probe: the arithmetic that re-derived the hurdle.

This module moved `slippage_bps` from 15/20 to 5/6 and so re-priced every closed
trip in the record (both `pnl` and `promotion` compute fees live from config).
A defect here would not raise -- it would quietly make the book look profitable,
which is the one failure this system must never have. So the walk is pinned
against books whose answer can be computed by hand.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from trading.accounting.slippage import Fill, SlippageProbe, walk
from trading.config import load_config


def test_walk_consumes_levels_in_order():
    """$300 against 1 unit at 100 and 10 at 110 -> 1@100 + 10/11@110."""
    vwap, filled = walk([["100", "1"], ["110", "10"]], 300.0)
    assert filled == pytest.approx(1.0)
    units = 1 + 200 / 110
    assert vwap == pytest.approx(300 / units)


def test_a_book_too_thin_reports_partial_fill_rather_than_clamping():
    """Clamping to the last level would price the THINNEST names as cheapest.

    That is the whole reason `insufficient` exists: a silent clamp inverts the
    ranking exactly where the cost is worst.
    """
    vwap, filled = walk([["100", "1"]], 500.0)
    assert vwap == pytest.approx(100.0)
    assert filled == pytest.approx(0.2)
    assert Fill("X", "CRYPTO", "BUY", 500.0, vwap, 100.0, 0.0, filled).insufficient


def test_an_empty_side_is_not_a_free_fill():
    vwap, filled = walk([], 100.0)
    assert (vwap, filled) == (0.0, 0.0)


def test_zero_and_negative_levels_are_skipped():
    """Binance has sent zero-quantity levels; they must not divide anything."""
    vwap, filled = walk([["0", "5"], ["100", "2"]], 100.0)
    assert vwap == pytest.approx(100.0)
    assert filled == pytest.approx(1.0)


class _Client:
    """A two-symbol book: one tight, one thin. No network."""

    BOOKS: ClassVar[dict] = {
        "TIGHT": {"bids": [["99.99", "1000"]], "asks": [["100.01", "1000"]]},
        "THIN": {"bids": [["90", "0.1"], ["80", "0.1"]], "asks": [["110", "0.1"]]},
    }

    def call(self, name, params):
        assert name == "depth"

        class _Page:
            body = _Client.BOOKS[params["symbol"]]

        return _Page()


@pytest.fixture
def probe():
    cfg = load_config()
    cfg.accounting.slippage_probe.notionals = [100.0]
    return SlippageProbe(cfg, client=_Client())


def test_a_tight_book_costs_about_the_spread(probe):
    """Both sides crossed on a 2bps spread is ~2bps round trip, not ~4."""
    fills = probe.measure("TIGHT", "CRYPTO", 100.0)
    round_trip = sum(f.slippage_bps for f in fills)
    assert round_trip == pytest.approx(2.0, abs=0.05)
    assert all(not f.insufficient for f in fills)


def test_slippage_is_positive_on_both_sides(probe):
    """A buy fills ABOVE mid and a sell BELOW it; both are costs.

    Signing one of them the other way would net them to nothing and report a
    frictionless book.
    """
    assert all(f.slippage_bps > 0 for f in probe.measure("TIGHT", "CRYPTO", 100.0))


def test_a_thin_symbol_is_excluded_rather_than_averaged_in(probe):
    """It must not be counted at its partial-fill price, which looks cheap."""
    fills = probe.measure("THIN", "CRYPTO", 100.0) + probe.measure("TIGHT", "CRYPTO", 100.0)
    cell = probe._summarise(fills)[0]
    assert cell["insufficient_depth"] == ["THIN"]
    assert cell["n_symbols"] == 1
    assert cell["round_trip_bps_median"] == pytest.approx(2.0, abs=0.05)


def test_the_configured_hurdle_is_what_the_report_compares_against(probe):
    """The comparison must read config, not a literal -- config is what changed."""
    cell = probe._summarise(probe.measure("TIGHT", "CRYPTO", 100.0))[0]
    expected = load_config().accounting.fees_for("CRYPTO").slippage_bps * 2
    assert cell["configured_round_trip_bps"] == expected
    assert cell["overstated_x"] == pytest.approx(expected / cell["round_trip_bps_median"], rel=1e-3)


def test_the_shipped_hurdle_still_exceeds_the_measured_cost():
    """The margin is the policy: this ledger overstates costs, never flatters.

    If a future re-measure ever sets slippage_bps BELOW what the probe found,
    the ledger starts reporting profit the account did not earn.
    """
    fees = load_config().accounting
    # Round-trip medians measured against the mainnet book on 2026-09-15 for the
    # random arm's pool -- the wider population, and the one that trades most.
    measured_pool = {"CRYPTO": 4.58, "BSTOCKS": 7.23}
    for book, measured in measured_pool.items():
        assert fees.fees_for(book).slippage_bps * 2 > measured, book
