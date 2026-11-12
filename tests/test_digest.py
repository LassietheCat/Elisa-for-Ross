from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from elisa_bot.digest import report_week, weekly_digest
from elisa_bot.state import BotState

NY = ZoneInfo("America/New_York")
MONDAY = dt.datetime(2026, 11, 16, 8, 0, tzinfo=NY)
WEEK = (dt.date(2026, 11, 9), dt.date(2026, 11, 16))


def event(at: str, slot: str | None, outcome: str, seconds: float | None = None) -> dict:
    return {"at": at, "slot": slot, "outcome": outcome, "seconds": seconds}


def test_a_full_week_reports_openings_rules_speed_checks_and_watching():
    state = BotState(
        activity=[
            event("2026-11-08T23:00:00-05:00", "2026-11-18 AM", "applied", 1.0),
            event("2026-11-09T09:00:00-05:00", "2026-11-20 PM", "applied", 1.4),
            event("2026-11-09T10:00:00-05:00", "2026-11-16 AM", "applied", 0.6),
            event("2026-11-10T11:00:00-05:00", "2026-11-27 PM", "applied", 2.0),
            event("2026-11-10T11:05:00-05:00", None, "gone", 1.9),
            event("2026-11-11T12:00:00-05:00", "2026-11-30 PM", "too close"),
            event("2026-11-12T13:00:00-05:00", "2026-11-30 PM", "day full"),
            event("2026-11-12T14:00:00-05:00", "2026-12-01 AM", "unverified", 2.4),
            event("2026-11-12T14:03:00-05:00", "2026-12-01 AM", "landed"),
            event("2026-11-13T15:00:00-05:00", "2026-12-04 PM", "missed", 3.1),
            event("2026-11-16T07:00:00-05:00", "2026-12-07 AM", "applied", 0.5),
        ],
        daily={
            "2026-11-08": {"reads": 900},
            "2026-11-09": {"reads": 1400, "failed": 2},
            "2026-11-12": {"reads": 1300, "on_calendar": 2},
            "2026-11-13": {"reads": 1350, "not_on_calendar": 1},
            "2026-11-16": {"reads": 50},
        },
        watch={
            "since": "2026-11-01T10:00:00-05:00",
            "last_ok": "2026-11-16T07:59:00-05:00",
            "gaps": [
                ["2026-11-08T23:50:00-05:00", "2026-11-09T00:30:00-05:00"],
                ["2026-11-11T03:10:00-05:00", "2026-11-11T03:40:00-05:00"],
                ["2026-11-15T22:00:00-05:00", "2026-11-16T01:00:00-05:00"],
            ],
        },
    )
    message = weekly_digest(state, *WEEK, MONDAY)
    assert message.subject == "Elisa: weekly report, Mon Nov 9 to Sun Nov 15"
    assert message.sms is False
    assert message.text == """Week of Mon Nov 9 to Sun Nov 15.

Openings the bot saw: 8
- Accepted: 3 (Mon Nov 16 AM, Fri Nov 20 PM, Fri Nov 27 PM)
- Accept clicked but not confirmed ("please check"): 1
- Taken by someone else first: 2
- Not accepted because of the rules: 2
    too close to another job: 1 (Mon Nov 30 PM)
    the day was already full: 1 (Mon Nov 30 PM)

Speed: accepted a median of 1.4 s after the bot noticed the opening (fastest 0.6 s).

Checked on the calendar after Accept: 2 there, 1 not there.

Watching: 98.2% of the week.
Gaps of 10 minutes or more:
- Mon Nov 9, 12:00 AM to 12:30 AM
- Wed Nov 11, 3:10 AM to 3:40 AM
- Sun Nov 15, 10:00 PM to Mon Nov 16, 12:00 AM
Calendar reads: 4,050 (another 2 failed and were retried).
"""


def test_the_first_week_says_when_measuring_began_and_counts_a_silence_still_going_on():
    state = BotState(watch={"since": "2026-11-12T15:10:00-05:00", "last_ok": "2026-11-15T20:00:00-05:00", "gaps": []})
    assert weekly_digest(state, *WEEK, MONDAY).text == """Week of Mon Nov 9 to Sun Nov 15.

Openings the bot saw: none.

Watching: 95% since Thu Nov 12, 3:10 PM, when this report began measuring.
Gaps of 10 minutes or more:
- Sun Nov 15, 8:00 PM to Mon Nov 16, 12:00 AM
"""


def test_a_week_still_running_and_never_measured_says_so():
    thursday = dt.datetime(2026, 11, 12, 10, 0, tzinfo=NY)
    assert weekly_digest(BotState(), *WEEK, thursday).text == (
        "Week of Mon Nov 9 to Sun Nov 15 (so far).\n\nOpenings the bot saw: none.\n\nWatching: not measured yet.\n"
    )


def test_a_long_list_of_gaps_is_cut_short():
    gaps = [[f"2026-11-10T0{h}:00:00-05:00", f"2026-11-10T0{h}:15:00-05:00"] for h in range(1, 8)]
    text = weekly_digest(BotState(watch={"since": "2026-11-01T00:00:00-05:00", "last_ok": MONDAY.isoformat(), "gaps": gaps}), *WEEK, MONDAY).text
    assert "- Tue Nov 10, 5:00 AM to 5:15 AM\n- and 2 more\n" in text
    assert "Watching: 98.9% of the week." in text


def test_the_report_for_a_week_is_due_from_eight_on_the_monday_after():
    assert report_week(dt.datetime(2026, 11, 16, 7, 59, tzinfo=NY)) == dt.date(2026, 11, 2)
    assert report_week(MONDAY) == dt.date(2026, 11, 9)
    assert report_week(dt.datetime(2026, 11, 15, 23, 0, tzinfo=NY)) == dt.date(2026, 11, 2)
    assert report_week(dt.datetime(2026, 11, 17, 6, 0, tzinfo=NY)) == dt.date(2026, 11, 9)
