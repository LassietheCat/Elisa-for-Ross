from __future__ import annotations

import datetime as dt

from elisa_bot.config import Settings
from elisa_bot.detect import plan_month
from elisa_bot.models import Slot, SlotTags
from elisa_bot.state import BotState

TODAY = dt.date(2026, 10, 7)
S = Slot.parse


def open_(n: int = 1) -> SlotTags:
    return SlotTags(available=n)


def test_first_run_only_records_a_baseline():
    plan = plan_month({S("2026-10-14 AM"): open_()}, BotState(), today=TODAY, settings=Settings())
    assert plan.baseline_only
    assert plan.to_apply == []
    assert plan.new == {S("2026-10-14 AM")}


def test_first_run_can_apply_when_configured():
    plan = plan_month({S("2026-10-14 AM"): open_()}, BotState(), today=TODAY, settings=Settings(apply_on_first_run=True))
    assert plan.to_apply == [S("2026-10-14 AM")]


def test_only_slots_not_seen_last_run_are_new():
    state = BotState(baseline_done=True, seen=["2026-10-14 AM"])
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-15 PM"): open_(), S("2026-10-16 AM"): SlotTags()}
    plan = plan_month(slots, state, today=TODAY, settings=Settings())
    assert plan.new == {S("2026-10-15 PM")}
    assert plan.to_apply == [S("2026-10-15 PM")]


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
    assert plan.to_apply == [S("2026-10-14 AM")]


def test_already_accepted_and_past_slots_are_ignored():
    state = BotState(baseline_done=True, applied={"2026-10-14 AM": "2026-10-06T09:00:00-04:00"})
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-06 PM"): open_()}
    assert plan_month(slots, state, today=TODAY, settings=Settings()).to_apply == []


def test_todays_slots_are_included():
    plan = plan_month({S("2026-10-07 PM"): open_()}, BotState(baseline_done=True), today=TODAY, settings=Settings())
    assert plan.to_apply == [S("2026-10-07 PM")]


def test_daily_cap_counts_earlier_accepts():
    state = BotState(baseline_done=True, applies_by_day={"2026-10-07": 1})
    slots = {S("2026-10-14 AM"): open_(), S("2026-10-15 AM"): open_(), S("2026-10-16 AM"): open_()}
    plan = plan_month(slots, state, today=TODAY, settings=Settings(max_applies_per_day=3), already_planned=1)
    assert plan.to_apply == [S("2026-10-14 AM")]


def test_zero_cap_means_no_cap():
    slots = {S(f"2026-10-{day} AM"): open_() for day in range(10, 25)}
    plan = plan_month(slots, BotState(baseline_done=True), today=TODAY, settings=Settings(max_applies_per_day=0))
    assert len(plan.to_apply) == 15


def test_used_up_tries_are_not_retried():
    state = BotState(baseline_done=True, attempts={"2026-10-14 AM": 2})
    assert plan_month({S("2026-10-14 AM"): open_()}, state, today=TODAY, settings=Settings()).to_apply == []
