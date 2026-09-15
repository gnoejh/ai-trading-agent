"""The KR trading day is two continuous sessions, not one, and it runs to 20:00.

Extended-hours trading (owner, 2026-09-15) is NOT a longer KRX day -- KRX still
ends at 15:30. The evening window lives on the alternative venue, so a correct
clock is only half the change: the screen's rankers and the order router have to
name a venue that is actually open at 18:00, and the two families of endpoint
spell that differently ("3" is 통합 on the rankers, "0" on the account calls).
These pin both halves, because a clock that opens a session the router cannot
reach would look exactly like a working extension until the first evening order.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from trading.config import Session, load_config

KST = ZoneInfo("Asia/Seoul")


def _at(hour: int, minute: int, day: int = 15) -> dt.datetime:
    """A weekday in KST. 2026-09-15 is a Tuesday."""
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=KST)


@pytest.fixture
def kr():
    return load_config().agent


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (8, 59, False),  # before the open
        (9, 0, True),  # day session
        (15, 19, True),  # last minute of continuous KRX trading
        (15, 20, False),  # closing auction -- not a continuous book
        (15, 39, False),  # venue changeover
        (15, 40, True),  # after-market opens
        (18, 0, True),  # the hours this change exists for
        (20, 0, True),  # close is inclusive, as it always was
        (20, 1, False),  # and the evening ends
    ],
)
def test_kr_day_has_two_continuous_sessions(kr, hour, minute, expected):
    assert kr.is_open("KR", _at(hour, minute)) is expected


def test_the_break_does_not_leak_into_other_venues(kr):
    """US has no break; the field defaults empty rather than being required."""
    assert load_config().agent.sessions["US"].breaks == []


def test_weekends_still_close_the_extended_session(kr):
    """2026-09-19 is a Saturday: the extension lengthens days, not the week."""
    assert kr.is_open("KR", _at(18, 0, day=19)) is False


def test_a_break_outside_the_session_cannot_reopen_it():
    """`breaks` only ever subtracts. A malformed window must not add hours."""
    s = Session(timezone="Asia/Seoul", open="09:00", close="15:20", breaks=[["21:00", "22:00"]])
    assert s.is_open(_at(21, 30)) is False


def test_binance_ignores_the_clock_entirely(kr):
    """always_open short-circuits before sessions are consulted."""
    assert kr.is_open("BINANCE", _at(3, 0)) is True


def test_kr_orders_route_where_the_evening_venue_can_fill():
    """KRX cannot fill after 15:30; SOR is the only value correct all day."""
    cfg = load_config()
    assert cfg.broker.kiwoom.market("KR").exchange == "SOR"


def test_kr_rankers_span_both_venues():
    """stex_tp "1" is KRX-only: every evening cycle would rank a frozen tape.

    The enum differs per endpoint family -- "3" is 통합 on ka10032/ka90009 while
    "0" is 통합 on ka10075/ka10076 -- so this asserts the ranker spelling, not a
    single shared constant.
    """
    kr = load_config().agent.screen.market("KR")
    for ranker in list(kr.rankers) + list(kr.pool_rankers):
        assert ranker.params.get("stex_tp") == "3", ranker.api_id
