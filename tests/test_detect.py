from __future__ import annotations

import datetime as dt

from zoneinfo import ZoneInfo

from elisa_bot.config import Settings
from elisa_bot.detect import openings_in, plan_month
from elisa_bot.models import Opening, OpenOrders, Slot, SlotTags
from elisa_bot.state import BotState

TODAY = dt.date(2026, 10, 7)
S = Slot.parse
NEW_YORK = ZoneInfo("America/New_York")


def slots_of(openings: list[Opening]) -> list[Slot]:
    return [opening.slot for opening in openings]


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=NEW_YORK)


def open_(n: int = 1) -> SlotTags:
    return SlotTags(available=n)


def test_first_run_only_records_a_baseline():
    plan = plan_month({S("2026-10-14 AM"): open_()}, BotState(), today=TODAY, settings=Settings())
    assert plan.baseline_only
    assert plan.to_apply == []
    assert plan.new == {S("2026-10-14 AM")}


def test_first_run_can_apply_when_configured():
    plan = plan_month({S("2026-10-14 AM"): open_()}, BotState(), today=TODAY, settings=Settings(apply_on_first_run=True))
    assert slots_of(plan.to_apply) == [S("2026-10-14 AM")]


def test_only_slots_not_seen_last_run_are_new():
    state = BotState(baseline_done=True, seen=["2026-10-14 AM"])
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-15 PM"): open_(), S("2026-10-16 AM"): SlotTags()}
    plan = plan_month(slots, state, today=TODAY, settings=Settings())
    assert plan.new == {S("2026-10-15 PM")}
    assert slots_of(plan.to_apply) == [S("2026-10-15 PM")]


def test_slot_with_an_assigned_or_confirmed_job_is_skipped():
    state = BotState(baseline_done=True)
    slots = {S("2026-10-15 AM"): SlotTags(available=1, assigned=1), S("2026-10-16 PM"): SlotTags(available=1, confirmed=1)}
    plan = plan_month(slots, state, today=TODAY, settings=Settings())
    assert plan.to_apply == []
    assert plan.skipped_booked == [S("2026-10-15 AM"), S("2026-10-16 PM")]


def test_a_pending_application_counts_as_booked():
    plan = plan_month({S("2026-10-15 AM"): SlotTags(available=1, applied=1)}, BotState(baseline_done=True), today=TODAY, settings=Settings())
    assert plan.to_apply == [] and plan.skipped_booked == [S("2026-10-15 AM")]


def test_two_openings_in_one_slot_are_one_slot():
    plan = plan_month({S("2026-10-14 AM"): open_(2)}, BotState(baseline_done=True), today=TODAY, settings=Settings())
    assert slots_of(plan.to_apply) == [S("2026-10-14 AM")]


def test_already_accepted_and_past_slots_are_ignored():
    state = BotState(baseline_done=True, applied={"2026-10-14 AM": "2026-10-06T09:00:00-04:00"})
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-06 PM"): open_()}
    assert plan_month(slots, state, today=TODAY, settings=Settings()).to_apply == []


def test_todays_slots_are_included():
    plan = plan_month({S("2026-10-07 PM"): open_()}, BotState(baseline_done=True), today=TODAY, settings=Settings())
    assert slots_of(plan.to_apply) == [S("2026-10-07 PM")]


def test_daily_cap_counts_earlier_accepts():
    state = BotState(baseline_done=True, applies_by_day={"2026-10-07": 1})
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-15 AM"): open_(), S("2026-10-16 AM"): open_()}
    plan = plan_month(slots, state, today=TODAY, settings=Settings(max_applies_per_day=3), already_planned=1)
    assert slots_of(plan.to_apply) == [S("2026-10-14 AM")]


def test_zero_cap_means_no_cap():
    slots = {S(f"2026-10-{day} AM"): open_() for day in range(10, 25)}
    plan = plan_month(slots, BotState(baseline_done=True), today=TODAY, settings=Settings(max_applies_per_day=0))
    assert len(plan.to_apply) == 15


def test_used_up_tries_are_not_retried():
    state = BotState(baseline_done=True, attempts={"2026-10-14 AM": 2})
    assert plan_month({S("2026-10-14 AM"): open_()}, state, today=TODAY, settings=Settings()).to_apply == []


def test_three_months_are_checked_and_read_from_the_month_on_screen():
    from elisa_bot.cycle import reading_order, target_months

    months = target_months(dt.date(2026, 11, 20), 3)
    assert months == [(2026, 11), (2026, 12), (2027, 1)]
    assert reading_order(months, (2026, 11)) == [(2026, 11), (2026, 12), (2027, 1)]
    assert reading_order(months, (2027, 1)) == [(2027, 1), (2026, 12), (2026, 11)]
    assert reading_order(months, (2026, 12)) == [(2026, 12), (2027, 1), (2026, 11)]
    assert reading_order(months, (2026, 10)) == months


def test_a_day_off_is_never_accepted():
    plan = plan_month(
        {S("2026-10-14 AM"): open_(), S("2026-10-15 PM"): open_()},
        BotState(baseline_done=True),
        today=TODAY,
        settings=Settings(),
        days_off={S("2026-10-14 AM"), S("2026-10-14 PM")},
    )
    assert slots_of(plan.to_apply) == [S("2026-10-15 PM")] and plan.skipped_day_off == [S("2026-10-14 AM")]


def test_a_half_day_off_blocks_only_jobs_starting_in_that_half():
    slots = {S("2026-11-10 AM"): open_(), S("2026-11-10 PM"): open_()}
    openings = openings_in(slots, [at("2026-11-10T11:30"), at("2026-11-10T15:00")])
    plan = plan_month(
        slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings,
        job_times={}, days_off={S("2026-11-10 AM")},
    )
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-10T15:00")]
    assert plan.skipped_day_off == [S("2026-11-10 AM")]
    plan = plan_month(
        slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings,
        job_times={}, days_off={S("2026-11-10 PM")},
    )
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-10T11:30")]
    assert plan.skipped_day_off == [S("2026-11-10 PM")]


def test_openings_come_from_elisas_list_and_badges_only_count_when_the_list_is_missing():
    slots = {S("2026-11-10 AM"): open_(), S("2026-11-10 PM"): open_(), S("2026-11-13 AM"): SlotTags()}
    openings = openings_in(slots, [at("2026-11-10T13:00"), at("2026-11-13T09:00")])
    assert openings == [Opening(S("2026-11-10 PM"), at("2026-11-10T13:00"))]
    assert openings_in(slots, None) == [Opening(S("2026-11-10 AM")), Opening(S("2026-11-10 PM"))]


def test_a_second_job_the_same_day_needs_three_hours_between_start_times():
    slots = {S("2026-11-10 AM"): SlotTags(available=1, assigned=1), S("2026-11-10 PM"): open_(2)}
    openings = openings_in(slots, [at("2026-11-10T10:00"), at("2026-11-10T13:00"), at("2026-11-10T15:30")])
    plan = plan_month(
        slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings,
        job_times={dt.date(2026, 11, 10): [at("2026-11-10T09:00")]},
    )
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-10T13:00")]
    assert plan.skipped_too_close == [S("2026-11-10 AM"), S("2026-11-10 PM")]


def test_two_jobs_in_one_afternoon_and_no_more_than_the_daily_maximum():
    slots = {S("2026-11-12 AM"): SlotTags(confirmed=1), S("2026-11-12 PM"): open_(3)}
    openings = openings_in(slots, [at("2026-11-12T12:30"), at("2026-11-12T15:30"), at("2026-11-12T18:30")])
    times = {dt.date(2026, 11, 12): [at("2026-11-12T08:00")]}
    plan = plan_month(slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings, job_times=times)
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-12T12:30"), at("2026-11-12T15:30")]
    assert plan.skipped_day_full == [S("2026-11-12 PM")]
    plan = plan_month(
        slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings, job_times=times, max_jobs=2
    )
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-12T12:30")]
    assert plan.skipped_day_full == [S("2026-11-12 PM"), S("2026-11-12 PM")]


def test_without_her_job_times_only_a_free_half_day_is_accepted():
    slots = {S("2026-11-16 AM"): SlotTags(available=1, assigned=1), S("2026-11-16 PM"): open_()}
    openings = openings_in(slots, [at("2026-11-16T10:00"), at("2026-11-16T14:00")])
    for times in (None, []):
        plan = plan_month(
            slots, BotState(baseline_done=True), today=TODAY, settings=Settings(), openings=openings,
            job_times={dt.date(2026, 11, 16): times},
        )
        assert slots_of(plan.to_apply) == [S("2026-11-16 PM")] and plan.skipped_booked == [S("2026-11-16 AM")]


def test_an_opening_in_an_already_listed_half_day_is_new_only_if_its_time_is():
    state = BotState(baseline_done=True, seen=["2026-11-17 PM"])
    slots = {S("2026-11-17 PM"): open_(2)}
    openings = openings_in(slots, [at("2026-11-17T13:00"), at("2026-11-17T16:30")])
    times = {dt.date(2026, 11, 17): []}
    known = {S("2026-11-17 PM"): {"01:00 PM"}}
    plan = plan_month(slots, state, today=TODAY, settings=Settings(), openings=openings, known=known, job_times=times)
    assert [opening.start for opening in plan.to_apply] == [at("2026-11-17T16:30")]
    assert plan_month(slots, state, today=TODAY, settings=Settings(), openings=openings, job_times=times).to_apply == []
    clicked = {(S("2026-11-17 PM"), "04:30 PM")}
    assert plan_month(
        slots, state, today=TODAY, settings=Settings(), openings=openings, known=known, clicked=clicked, job_times=times
    ).to_apply == []


def test_when_elisas_list_loaded_a_badge_without_an_open_order_is_not_an_opening():
    assert openings_in({S("2026-11-16 AM"): open_()}, []) == []


def test_a_minimum_notice_skips_openings_that_start_too_soon():
    slots = {S("2026-10-07 PM"): open_(2)}
    now = at("2026-10-07T12:00")
    openings = openings_in(slots, [at("2026-10-07T13:30"), at("2026-10-07T17:00")])
    state = BotState(baseline_done=True)
    settings = Settings(min_notice_minutes=120)
    plan = plan_month(slots, state, today=TODAY, settings=settings, now=now, openings=openings, job_times={TODAY: []})
    assert [opening.start for opening in plan.to_apply] == [at("2026-10-07T17:00")]
    assert plan.skipped_too_soon == [S("2026-10-07 PM")]
    untimed = plan_month(slots, state, today=TODAY, settings=settings, now=now)
    assert untimed.to_apply == [] and untimed.skipped_too_soon == [S("2026-10-07 PM")]
    plan = plan_month(slots, state, today=TODAY, settings=Settings(), now=now, openings=openings, job_times={TODAY: []})
    assert plan.skipped_too_soon == [] and len(plan.to_apply) == 2



def test_badges_count_only_on_the_date_of_an_order_without_a_time():
    slots = {S("2026-11-16 AM"): open_(), S("2026-11-20 AM"): open_(), S("2026-11-20 PM"): open_()}
    orders = OpenOrders((at("2026-11-20T09:30"),), frozenset({dt.date(2026, 11, 20)}))
    assert openings_in(slots, orders) == [Opening(S("2026-11-20 AM"), at("2026-11-20T09:30")), Opening(S("2026-11-20 PM"))]


def test_minimum_notice_uses_the_half_days_earliest_start_without_a_time():
    slots = {S("2026-10-08 AM"): open_(), S("2026-10-08 PM"): open_()}
    state = BotState(baseline_done=True)
    late = at("2026-10-07T22:00")
    plan = plan_month(slots, state, today=TODAY, settings=Settings(min_notice_minutes=1440), now=late)
    assert plan.skipped_too_soon == [S("2026-10-08 AM"), S("2026-10-08 PM")]
    plan = plan_month(slots, state, today=TODAY, settings=Settings(min_notice_minutes=240), now=late)
    assert plan.skipped_too_soon == [S("2026-10-08 AM")] and slots_of(plan.to_apply) == [S("2026-10-08 PM")]
