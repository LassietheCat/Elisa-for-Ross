from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from elisa_bot.config import Settings
from elisa_bot.cycle import LANDING_WAIT_S, _check_landings, due_landing_month
from elisa_bot.models import LandingCheck, MonthReading, Slot, SlotTags
from elisa_bot.state import BotState, StateStore

S = Slot.parse
ACCEPTED = dt.datetime(2026, 11, 10, 17, 31, tzinfo=ZoneInfo("America/New_York"))


def later(seconds: float) -> dt.datetime:
    return ACCEPTED + dt.timedelta(seconds=seconds)


def november(jobs: dict[str, SlotTags]) -> MonthReading:
    return MonthReading(2026, 11, {S(text): tags for text, tags in jobs.items()})


def expecting(*accepts: tuple[str, int], unsure: bool = False) -> BotState:
    state = BotState()
    for slot, before in accepts:
        state.expect_on_calendar(S(slot), before, ACCEPTED, unsure=unsure)
    return state


def test_jobs_elisa_files_under_the_other_half_of_the_day_still_count_as_landed():
    state = expecting(("2026-11-27 PM", 1), ("2026-11-27 PM", 1))
    reading = november({"2026-11-27 AM": SlotTags(assigned=2), "2026-11-27 PM": SlotTags(assigned=1)})
    assert _check_landings(state, reading, later(4)) == [
        LandingCheck(dt.date(2026, 11, 27), (S("2026-11-27 PM"), S("2026-11-27 PM")), 0, False, 1)
    ]
    assert state.landing == {} and state.activity == []


def test_a_job_on_the_calendar_is_checked_off_at_the_first_read_without_waiting():
    state = expecting(("2026-11-12 AM", 0))
    checks = _check_landings(state, november({"2026-11-12 AM": SlotTags(assigned=1)}), later(2))
    assert [check.missing for check in checks] == [0]
    assert state.landing == {} and state.activity == []


def test_a_job_missing_from_the_calendar_gets_three_minutes_then_is_reported(caplog):
    caplog.set_level(logging.INFO)
    state = expecting(("2026-11-12 AM", 0))
    empty = november({})
    assert _check_landings(state, empty, later(LANDING_WAIT_S - 1)) == []
    assert list(state.landing) == ["2026-11-12"]
    assert _check_landings(state, empty, later(LANDING_WAIT_S)) == [
        LandingCheck(dt.date(2026, 11, 12), (S("2026-11-12 AM"),), 1, False, 3)
    ]
    assert state.landing == {}
    assert [(e["slot"], e["outcome"]) for e in state.activity] == [("2026-11-12 AM", "not landed")]
    assert "Thu Nov 12 AM: 1 of 1 accepted job(s) not on the calendar 3 min after Accept" in caplog.text


def test_a_second_job_on_a_day_that_already_had_one_has_to_raise_the_count():
    state = expecting(("2026-11-13 AM", 1))
    one_job = november({"2026-11-13 AM": SlotTags(assigned=1)})
    assert _check_landings(state, one_job, later(LANDING_WAIT_S))[0].missing == 1


def test_only_the_jobs_that_did_not_land_count_as_missing_and_never_more_than_were_accepted():
    state = expecting(("2026-11-27 PM", 1), ("2026-11-27 PM", 1))
    assert _check_landings(state, november({"2026-11-27 AM": SlotTags(assigned=2)}), later(LANDING_WAIT_S))[0].missing == 1
    state = expecting(("2026-11-30 AM", 2))
    assert _check_landings(state, november({}), later(LANDING_WAIT_S))[0].missing == 1


def test_a_please_check_accept_that_landed_is_recorded_as_on_the_calendar_after_all():
    state = expecting(("2026-11-12 AM", 0), unsure=True)
    checks = _check_landings(state, november({"2026-11-12 AM": SlotTags(applied=1)}), later(60))
    assert [(check.missing, check.unsure) for check in checks] == [(0, True)]
    assert [(e["slot"], e["outcome"]) for e in state.activity] == [("2026-11-12 AM", "landed")]


def test_a_check_waits_for_a_read_of_its_own_month_and_unreadable_ones_are_dropped(caplog):
    state = expecting(("2026-12-03 PM", 0))
    state.landing["2026-11-05"] = {"expect": "lots"}
    state.landing["someday"] = {"expect": 1, "at": ACCEPTED.isoformat(), "slots": ["2026-11-05 AM"]}
    assert _check_landings(state, november({}), later(3600)) == []
    assert list(state.landing) == ["2026-12-03"]
    assert "Dropped an unreadable calendar check (2026-11-05)" in caplog.text


def test_the_month_with_the_longest_overdue_check_is_read_next(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert due_landing_month(settings, ACCEPTED) is None
    state = BotState()
    state.expect_on_calendar(S("2026-12-03 PM"), 0, ACCEPTED, unsure=False)
    state.expect_on_calendar(S("2026-11-12 AM"), 0, later(60), unsure=False)
    state.landing["garbage"] = {"at": "not a time"}
    StateStore(settings.state_path).save(state)
    assert due_landing_month(settings, later(LANDING_WAIT_S - 1)) is None
    assert due_landing_month(settings, later(LANDING_WAIT_S)) == (2026, 12)
    assert due_landing_month(settings, later(LANDING_WAIT_S + 60)) == (2026, 12)


def test_each_check_is_counted_for_the_weekly_report():
    state = expecting(("2026-11-27 PM", 1), ("2026-11-27 PM", 1), ("2026-11-12 AM", 0))
    _check_landings(state, november({"2026-11-27 AM": SlotTags(assigned=2)}), later(LANDING_WAIT_S))
    assert state.daily == {"2026-11-10": {"on_calendar": 1, "not_on_calendar": 2}}


def test_on_a_mixed_day_the_please_check_job_is_the_one_named_missing():
    state = BotState()
    state.expect_on_calendar(S("2026-11-27 AM"), 0, ACCEPTED, unsure=False)
    state.expect_on_calendar(S("2026-11-27 PM"), 0, ACCEPTED, unsure=True)
    checks = _check_landings(state, november({"2026-11-27 AM": SlotTags(assigned=1)}), later(LANDING_WAIT_S))
    assert [(c.missing, c.unsure) for c in checks] == [(1, True)]
    assert [(e["slot"], e["outcome"]) for e in state.activity] == [("2026-11-27 PM", "not landed")]


def test_every_please_check_job_that_landed_gets_its_own_entry():
    state = expecting(("2026-11-27 PM", 0), ("2026-11-27 PM", 0), unsure=True)
    _check_landings(state, november({"2026-11-27 PM": SlotTags(assigned=2)}), later(30))
    assert [(e["slot"], e["outcome"]) for e in state.activity] == [("2026-11-27 PM", "landed"), ("2026-11-27 PM", "landed")]
