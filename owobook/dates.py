"""Date helpers. Stored timestamps are UTC; everything the trader sees is Lagos time."""

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from owobook.config import get_settings

TZ = ZoneInfo(get_settings().timezone)

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTHS = {
    name.lower(): i for i, name in enumerate(calendar.month_name) if name
} | {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name} | {"sept": 9}
_SMALL_NUMBERS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_TOMORROW = r"(tomorrow|tomorow|tommorow|tommorrow|tmrw|tmr|tumoro|tumorrow)"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    """Convert a stored timestamp to Lagos time. Naive values (from SQLite) are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def today_local(now: datetime | None = None) -> date:
    """Today's date in Lagos."""
    return to_local(now or utcnow()).date()


def end_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, months: int) -> date:
    """Same day N months later, clamped to the month's last day (31 Jan + 1 → 28/29 Feb)."""
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def next_weekday(today: date, weekday: int) -> date:
    """Next occurrence of a weekday strictly after today ('Friday' said on a Friday = next week)."""
    days_ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _explicit_date(s: str, today: date) -> date | None:
    """ISO (2026-11-05), day-first numeric (5/11, 5/11/2026), or '5th Nov' / 'Nov 5'."""
    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))

    year: int | None = None
    if m := re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", s):
        day, month = int(m[1]), int(m[2])  # Nigeria writes day first
        if m[3]:
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    else:
        month_words = "|".join(sorted(_MONTHS, key=len, reverse=True))
        day_pat = r"(\d{1,2})(?:st|nd|rd|th)?"
        if m := re.search(rf"\b{day_pat}\s+(?:of\s+)?({month_words})\b(?:\s+(\d{{4}}))?", s):
            day, month = int(m[1]), _MONTHS[m[2]]
        elif m := re.search(rf"\b({month_words})\s+{day_pat}\b(?:\s*,?\s*(\d{{4}}))?", s):
            day, month = int(m[2]), _MONTHS[m[1]]
        else:
            return None
        if m[3]:
            year = int(m[3])

    if year is not None:
        return _safe_date(year, month, day)
    d = _safe_date(today.year, month, day)
    if d and d < today:  # no year given and already passed → they mean next year
        d = _safe_date(today.year + 1, month, day)
    return d


def resolve_due_date(text: str, today: date | None = None) -> date | None:
    """Turn a spoken due date into a calendar date in Lagos, or None if not understood.

    Understands English and Pidgin: "today", "tomorrow", "next tomorrow" (day after tomorrow),
    "Friday" (next occurrence), "next week" (+7 days), "end of month", "next month",
    "in 3 days", "two weeks time", "weekend", and explicit dates like "5/11" or "5th Nov".
    """
    today = today or today_local()
    s = re.sub(r"[^\w\s/.-]", " ", text.lower())
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None

    if d := _explicit_date(s, today):
        return d
    if re.search(rf"\b(next {_TOMORROW}|day after {_TOMORROW})\b", s):
        return today + timedelta(days=2)
    if re.search(rf"\b{_TOMORROW}\b", s):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|this evening|now now)\b", s):
        return today
    if re.search(r"\b(end of (the )?month|month end|month ending)\b", s):
        return end_of_month(today)
    if re.search(r"\bnext month\b", s):
        return add_months(today, 1)

    numbers = "|".join(_SMALL_NUMBERS)
    if m := re.search(rf"\b(\d+|{numbers}) (day|days|week|weeks)\b", s):
        n = int(m[1]) if m[1].isdigit() else _SMALL_NUMBERS[m[1]]
        return today + timedelta(days=n * (7 if m[2].startswith("week") else 1))

    weekday_words = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
    if m := re.search(rf"\b({weekday_words})\b", s):
        if re.search(r"\bnext week\b", s):  # "Friday next week" → that weekday in next calendar week
            next_monday = today + timedelta(days=7 - today.weekday())
            return next_monday + timedelta(days=_WEEKDAYS[m[1]])
        return next_weekday(today, _WEEKDAYS[m[1]])
    if re.search(r"\bnext week\b", s):
        return today + timedelta(days=7)
    if re.search(r"\b(weekend|end of (the )?week)\b", s):
        return today if today.weekday() == 5 else next_weekday(today, 5)
    return None


def format_date(d: date, today: date | None = None) -> str:
    """'Fri, 30 Oct' — with the year added only when it isn't this year."""
    today = today or today_local()
    text = f"{d:%a}, {d.day} {d:%b}"
    return text if d.year == today.year else f"{text} {d.year}"


PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month")


def period_bounds(period: str, today: date | None = None) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of a reporting period, as UTC datetimes.

    Weeks start on Monday. Boundaries are Lagos midnights.
    """
    today = today or today_local()
    if period == "today":
        start, end = today, today + timedelta(days=1)
    elif period == "yesterday":
        start, end = today - timedelta(days=1), today
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif period == "last_week":
        end = today - timedelta(days=today.weekday())
        start = end - timedelta(days=7)
    elif period == "this_month":
        start, end = today.replace(day=1), add_months(today.replace(day=1), 1)
    elif period == "last_month":
        end = today.replace(day=1)
        start = add_months(end, -1)
    else:
        raise ValueError(f"Unknown period {period!r}; expected one of {PERIODS}")

    def to_utc(d: date) -> datetime:
        return datetime.combine(d, time.min, tzinfo=TZ).astimezone(timezone.utc)

    return to_utc(start), to_utc(end)
