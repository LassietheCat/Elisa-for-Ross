from __future__ import annotations

import pytest

from elisa_bot.models import ApplyOutcome, Slot
from elisa_bot.site.applier import Applier
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
    mock.configure(confirm_dialog=True)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    applier = await make_applier(session, test_mode=False)
    result = await applier.apply_one(S("2026-10-14 AM"))
    assert result.outcome is ApplyOutcome.APPLIED
    assert mock.stats()["accept_calls"] == ["1"]


async def test_takes_only_one_of_two_openings_in_a_slot(mock, session):
    mock.configure(confirm_dialog=True)
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
    assert (result.outcome, result.code) == (ApplyOutcome.MISSED, "no_accept_button")
    assert mock.stats()["accept_calls"] == []
    assert await no_window_open(session)


async def test_an_apply_label_is_recognised_too(mock, session):
    mock.configure(button_label="Apply")
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
