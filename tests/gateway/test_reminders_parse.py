"""Natural-language reminder-time resolution: Sydney, DST, future, ambiguity."""

import datetime
import os
import time
from zoneinfo import ZoneInfo

from gateway.reminders import parse

SYD = ZoneInfo("Australia/Sydney")
WINTER = datetime.datetime(2026, 7, 19, 15, 0, tzinfo=SYD)   # AEST +10:00
SUMMER = datetime.datetime(2026, 12, 20, 15, 0, tzinfo=SYD)  # AEDT +11:00


def test_real_canary_in_two_minutes():
    r = parse.resolve("in two minutes", now=WINTER)
    assert r.ok
    assert r.dt == datetime.datetime(2026, 7, 19, 15, 2, tzinfo=SYD)
    assert r.dt.utcoffset() == datetime.timedelta(hours=10)


def test_tomorrow_morning_at_nine_resolves_absolute():
    r = parse.resolve("tomorrow morning at nine", now=WINTER)
    assert r.ok
    assert r.dt == datetime.datetime(2026, 7, 20, 9, 0, tzinfo=SYD)


def test_dst_offset_tracks_the_date_not_the_host():
    # Same wall-clock phrasing yields +10 in winter and +11 in summer.
    assert parse.resolve("tomorrow at 9am", now=WINTER).dt.utcoffset() == datetime.timedelta(hours=10)
    assert parse.resolve("tomorrow at 9am", now=SUMMER).dt.utcoffset() == datetime.timedelta(hours=11)


def test_resolution_is_independent_of_host_timezone():
    # Force a non-Sydney host timezone; the explicit base keeps the answer fixed.
    saved = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        r = parse.resolve("in two minutes", now=WINTER)
        assert r.ok
        assert r.dt == datetime.datetime(2026, 7, 19, 15, 2, tzinfo=SYD)
        assert "+10:00" in parse.to_rfc3339(r.dt)
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_prefers_future():
    # 9am has already passed at a 3pm base, so "at 9am" must land tomorrow.
    r = parse.resolve("at 9am", now=WINTER)
    assert r.ok
    assert r.dt.date() == datetime.date(2026, 7, 20)


def test_bare_hour_is_ambiguous():
    r = parse.resolve("at 9", now=WINTER)
    assert not r.ok and r.needs_clarification and r.reason == "ambiguous_meridiem"
    r2 = parse.resolve("at nine", now=WINTER)
    assert not r2.ok and r2.needs_clarification and r2.reason == "ambiguous_meridiem"


def test_date_without_time_asks_for_time():
    r = parse.resolve("tomorrow", now=WINTER)
    assert not r.ok and r.needs_clarification and r.reason == "missing_time"


def test_unparseable_asks_for_clarification():
    r = parse.resolve("when the cows come home", now=WINTER)
    assert not r.ok and r.needs_clarification


def test_rfc3339_and_human_forms():
    dt = parse.resolve("tomorrow at 9am", now=WINTER).dt
    assert parse.to_rfc3339(dt) == "2026-07-20T09:00:00+10:00"
    human = parse.human(dt)
    assert "Monday 20 July 2026" in human
    assert "9:00 AM" in human
    assert "Australia/Sydney" in human
