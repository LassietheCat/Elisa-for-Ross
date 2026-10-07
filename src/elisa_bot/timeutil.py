from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo


def now_in(tz_name: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz_name))


def to_iso(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.isoformat(timespec="seconds")


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
