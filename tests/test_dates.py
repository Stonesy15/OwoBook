from datetime import date, datetime, timezone

import pytest

from owobook.dates import (
    add_months,
    end_of_month,
    format_date,
    next_weekday,
    period_bounds,
    resolve_due_date,
    to_local,
    today_local,
)

TUE = date(2026, 10, 27)  # a Tuesday


@pytest.mark.parametrize(
    "text, expected",
    [
        ("today", date(2026, 10, 27)),
        ("tonight", date(2026, 10, 27)),
        ("tomorrow", date(2026, 10, 28)),
        ("tmrw", date(2026, 10, 28)),
        ("next tomorrow", date(2026, 10, 29)),  # Pidgin: day after tomorrow
        ("day after tomorrow", date(2026, 10, 29)),
        ("Friday", date(2026, 10, 30)),
        ("she go pay Friday", date(2026, 10, 30)),
        ("by fri", date(2026, 10, 30)),
        ("next Friday", date(2026, 10, 30)),  # upcoming Friday; receipt shows the date
        ("Friday next week", date(2026, 11, 6)),
        ("Monday", date(2026, 11, 2)),
        ("Tuesday", date(2026, 11, 3)),  # same weekday as today → next week
        ("next week", date(2026, 11, 3)),
        ("end of month", date(2026, 10, 31)),
        ("month end", date(2026, 10, 31)),
        ("end of the month", date(2026, 10, 31)),
        ("next month", date(2026, 11, 27)),
        ("in 3 days", date(2026, 10, 30)),
        ("two weeks time", date(2026, 11, 10)),
        ("a week", date(2026, 11, 3)),
        ("weekend", date(2026, 10, 31)),
        ("end of the week", date(2026, 10, 31)),
        ("2026-11-05", date(2026, 11, 5)),
        ("5/11", date(2026, 11, 5)),  # day first
        ("5/11/2026", date(2026, 11, 5)),
        ("05-11-26", date(2026, 11, 5)),
        ("5th November", date(2026, 11, 5)),
        ("5th of Nov", date(2026, 11, 5)),
        ("Nov 5", date(2026, 11, 5)),
        ("November 5th, 2027", date(2027, 11, 5)),
        ("20 Oct", date(2027, 10, 20)),  # already passed this year → next year
    ],
)
def test_resolve_due_date(text, expected):
    assert resolve_due_date(text, today=TUE) == expected


@pytest.mark.parametrize("text", ["", "soon", "when she get money", "31/02", "4.5k"])
def test_resolve_due_date_unknown(text):
    assert resolve_due_date(text, today=TUE) is None


def test_end_of_month_handles_leap_years():
    assert end_of_month(date(2028, 2, 10)) == date(2028, 2, 29)
    assert end_of_month(date(2026, 2, 10)) == date(2026, 2, 28)


def test_add_months_clamps():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)


def test_next_weekday_is_strictly_after_today():
    assert next_weekday(TUE, 1) == date(2026, 11, 3)
    assert next_weekday(TUE, 2) == date(2026, 10, 28)


def test_format_date():
    assert format_date(date(2026, 10, 30), today=TUE) == "Fri, 30 Oct"
    assert format_date(date(2027, 1, 4), today=TUE) == "Mon, 4 Jan 2027"


def test_lagos_is_utc_plus_one():
    late_utc = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)
    assert to_local(late_utc).hour == 0
    assert today_local(late_utc) == date(2026, 10, 28)
    # naive datetimes (SQLite) are treated as UTC
    assert to_local(datetime(2026, 10, 27, 23, 30)).date() == date(2026, 10, 28)


def test_period_bounds_use_lagos_midnight():
    start, end = period_bounds("today", today=TUE)
    assert start == datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "period, start_day, end_day",
    [
        ("yesterday", date(2026, 10, 26), date(2026, 10, 27)),
        ("this_week", date(2026, 10, 26), date(2026, 11, 2)),
        ("last_week", date(2026, 10, 19), date(2026, 10, 26)),
        ("this_month", date(2026, 10, 1), date(2026, 11, 1)),
        ("last_month", date(2026, 9, 1), date(2026, 10, 1)),
    ],
)
def test_period_bounds(period, start_day, end_day):
    start, end = period_bounds(period, today=TUE)
    assert to_local(start).date() == start_day and to_local(start).hour == 0
    assert to_local(end).date() == end_day and to_local(end).hour == 0


def test_period_bounds_rejects_unknown():
    with pytest.raises(ValueError):
        period_bounds("forever", today=TUE)
