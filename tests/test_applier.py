from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo
import pytest
from playwright.async_api import Error as PlaywrightError

from elisa_bot.models import ApplyOutcome, Opening, Slot
from elisa_bot.site import applier as applier_module
from elisa_bot.site.applier import Applier, describe_refusal
from elisa_bot.site.calendar import CalendarReader
from elisa_bot.site.selectors import Selectors
from tests.conftest import wo

pytestmark = pytest.mark.browser
S = Slot.parse


async def make_applier(session, *, test_mode: bool) -> Applier:
    await session.ensure_ready()
    reader = CalendarReader(session.page, Selectors())
    await reader.refresh()
    return Applier(session.page, Selectors(), reader, test_mode=test_mode)


async def no_window_open(session) -> bool:
    return await session.page.locator(".modal.show").count() == 0


async def test_test_mode_finds_accept_but_does_not_click(mock, session):
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=True)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.TEST_MODE and not result.clicked
    stats = mock.stats()
    assert stats["details_opened"] == 1
    assert stats["accept_calls"] == []
    assert await no_window_open(session)


async def test_accepts_the_opening(mock, session):
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED and result.clicked
    assert mock.stats()["accept_calls"] == ["1"]
    assert await no_window_open(session)


async def test_answers_an_are_you_sure_window(mock, session):
    mock.configure(details_style="modal", confirm_dialog=True)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1"]


async def test_takes_only_one_of_two_openings_in_a_slot(mock, session):
    mock.configure(details_style="modal", confirm_dialog=True)
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1"]


async def test_taken_first_is_reported_as_missed(mock, session):
    mock.configure(apply_mode="taken")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.MISSED, "taken", True)


async def test_site_error_after_the_click_asks_the_owner_to_check(mock, session):
    mock.configure(apply_mode="error")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.UNVERIFIED, "site_reported_problem", True)


async def test_no_accept_button(mock, session):
    mock.configure(apply_mode="no_button")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.code) == (ApplyOutcome.ERROR, "no_accept_button")
    assert mock.stats()["accept_calls"] == []
    assert await no_window_open(session)


async def test_an_apply_label_is_recognised_too(mock, session):
    mock.configure(details_style="modal", button_label="Apply")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=True)
    assert (await applier.apply_one(S("2026-10-14 AM"))).outcome is ApplyOutcome.TEST_MODE


async def test_opening_next_month(mock, session):
    mock.set_work_orders([wo("2026-11-03", "PM", "available")])
    applier = await make_applier(session, test_mode=False)
    assert (await applier.apply_one(S("2026-11-03 PM"))).outcome is ApplyOutcome.APPLIED


async def test_opening_in_the_last_row_behind_the_footer(mock, session):
    mock.set_work_orders([wo("2026-10-31", "PM", "available")])
    applier = await make_applier(session, test_mode=False)
    assert (await applier.apply_one(S("2026-10-31 PM"))).outcome is ApplyOutcome.APPLIED


async def test_opening_already_gone(mock, session):
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.MISSED, "gone", False)


def flaky_recheck(monkeypatch, failures: int) -> list:
    original = Applier._recheck
    calls = []

    async def recheck(self, slot):
        calls.append(slot)
        if len(calls) <= failures:
            raise PlaywrightError("Timeout 15000ms exceeded")
        return await original(self, slot)

    monkeypatch.setattr(Applier, "_recheck", recheck)
    return calls


async def test_a_slow_result_is_looked_at_once_more_before_asking_the_owner_to_check(mock, session, monkeypatch):
    mock.configure(details_style="modal")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    calls = flaky_recheck(monkeypatch, failures=1)
    result = await applier.apply_one(Slot.parse("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED and result.clicked and len(calls) == 2


async def test_two_failed_looks_ask_the_owner_to_check(mock, session, monkeypatch):
    mock.configure(details_style="modal")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    calls = flaky_recheck(monkeypatch, failures=2)
    result = await applier.apply_one(Slot.parse("2026-10-14 AM"))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.UNVERIFIED, "check_failed", True)
    assert len(calls) == 2 and len(mock.stats()["accept_calls"]) == 1


async def test_the_real_details_list_accepts_by_elisas_own_answer(mock, session):
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.clicked) == (ApplyOutcome.APPLIED, True)
    assert mock.stats()["accept_calls"] == ["1"]
    assert await session.page.locator("#wo-panel").is_visible()


async def test_the_on_site_driving_distance_prompt_is_answered(mock, session):
    mock.configure(distance_prompt=True)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1"]
    assert await no_window_open(session)


async def test_a_label_elisa_fills_in_itself_does_not_matter(mock, session):
    mock.configure(button_label="VRI")
    mock.set_work_orders([wo("2026-10-14", "PM", "available")])
    applier = await make_applier(session, test_mode=False)
    assert (await applier.apply_one(S("2026-10-14 PM"))).outcome is ApplyOutcome.APPLIED


async def test_a_refusal_from_elisa_is_reported_as_refused_with_its_wording_masked(mock, session, caplog):
    caplog.set_level(logging.INFO)
    mock.configure(apply_mode="refuse")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.REFUSED, "refused", True)
    assert "Elisa refused the Accept for 2026-10-14 AM: request could not be completed for coi [" in caplog.text
    assert "9990001" not in caplog.text



def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("America/New_York"))


async def test_after_a_refusal_the_next_order_at_the_same_time_is_taken(mock, session):
    mock.set_work_orders([wo("2026-10-14", "AM", "assigned_listed", "09:00"), wo("2026-10-14", "AM", "available", "09:00")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(Opening(S("2026-10-14 AM"), at("2026-10-14T09:00")))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1", "2"]


async def test_a_taken_order_in_another_court_is_skipped_for_the_open_one(mock, session):
    mock.set_work_orders([
        wo("2026-10-14", "AM", "assigned_listed", "09:00", "AAA"),
        wo("2026-10-14", "AM", "available", "09:00", "BBB"),
    ])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(Opening(S("2026-10-14 AM"), at("2026-10-14T09:00")))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1", "2"]


async def test_no_answer_from_elisa_asks_to_check_even_if_that_half_day_has_another_job(mock, session, monkeypatch):
    monkeypatch.setattr(applier_module, "ACCEPT_ANSWER_S", 0.5)
    mock.configure(accept_delay_ms=3000)
    mock.set_work_orders([wo("2026-10-14", "AM", "assigned", "07:00"), wo("2026-10-14", "AM", "available", "11:30")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(Opening(S("2026-10-14 AM"), at("2026-10-14T11:30")))
    assert (result.outcome, result.code, result.clicked) == (ApplyOutcome.UNVERIFIED, "no_answer", True)


def test_a_refusal_is_logged_without_names_courts_or_numbers():
    text = describe_refusal("COI 1234567 is assigned to Maria Lopez (Spanish) at Bronx Criminal Court, Judge R. Ortiz")
    assert text.startswith("coi is assigned to … at … [88 chars, #")
    for detail in ("1234567", "Maria", "Lopez", "Spanish", "Bronx", "Court", "Ortiz"):
        assert detail not in text
    assert describe_refusal("").startswith("no reason given [0 chars")


async def test_only_a_row_of_a_wanted_job_type_is_accepted(mock, session):
    mock.set_work_orders([
        wo("2026-10-14", "AM", "available", "09:00", sertype="OSI"),
        wo("2026-10-14", "AM", "available", "09:00", sertype="VRI"),
        wo("2026-10-15", "PM", "available", "14:00", sertype="OPI"),
    ])
    await session.ensure_ready()
    reader = CalendarReader(session.page, Selectors())
    await reader.refresh()
    applier = Applier(session.page, Selectors(), reader, test_mode=False, service_types=frozenset({"VRI"}))
    result = await applier.apply_one(Opening(S("2026-10-14 AM"), at("2026-10-14T09:00")))
    assert result.outcome is ApplyOutcome.APPLIED and mock.stats()["accept_calls"] == ["2"]
    result = await applier.apply_one(Opening(S("2026-10-15 PM"), at("2026-10-15T14:00")))
    assert (result.outcome, result.clicked) == (ApplyOutcome.ERROR, False)
    assert mock.stats()["accept_calls"] == ["2"]
