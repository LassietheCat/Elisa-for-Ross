from __future__ import annotations

import pytest

from elisa_bot.models import Slot, SlotTags
from elisa_bot.site.calendar import CalendarReader, parse_month_title
from elisa_bot.site.errors import CalendarNotRendered
from elisa_bot.site.selectors import Selectors
from tests.conftest import wo

pytestmark = pytest.mark.browser
S = Slot.parse


async def ready_reader(session) -> CalendarReader:
    await session.ensure_ready()
    reader = CalendarReader(session.page, Selectors())
    await reader.refresh()
    return reader


def test_month_titles():
    assert parse_month_title("October 2026") == (2026, 10)
    assert parse_month_title("  january 2027 ") == (2027, 1)
    assert parse_month_title("Available") is None


async def test_reads_each_status_into_the_right_day_and_half(mock, session):
    mock.set_work_orders([
        wo("2026-10-14", "AM", "available"),
        wo("2026-10-09", "AM", "assigned"),
        wo("2026-10-09", "AM", "assigned"),
        wo("2026-10-05", "PM", "confirmed"),
        wo("2026-10-20", "PM", "applied"),
        wo("2026-10-31", "PM", "available"),
    ])
    reader = await ready_reader(session)
    reading = await reader.read_displayed_month()
    assert (reading.year, reading.month) == (2026, 10)
    assert len(reading.slots) == 31 * 2
    assert reading.slots[S("2026-10-14 AM")] == SlotTags(available=1)
    assert reading.slots[S("2026-10-14 PM")] == SlotTags()
    assert reading.slots[S("2026-10-09 AM")] == SlotTags(assigned=2)
    assert reading.slots[S("2026-10-05 PM")] == SlotTags(confirmed=1)
    assert reading.slots[S("2026-10-05 AM")] == SlotTags()
    assert reading.slots[S("2026-10-20 PM")] == SlotTags(applied=1)
    assert reading.slots[S("2026-10-12 AM")] == SlotTags()
    assert reading.available() == {S("2026-10-14 AM"), S("2026-10-31 PM")}


async def test_reads_this_month_and_next(mock, session):
    mock.set_work_orders([wo("2026-10-22", "PM", "available"), wo("2026-11-03", "AM", "available")])
    reader = await ready_reader(session)
    slots = await reader.read_months([(2026, 10), (2026, 11)])
    assert len(slots) == (31 + 30) * 2
    assert {slot for slot, tags in slots.items() if tags.available} == {S("2026-10-22 PM"), S("2026-11-03 AM")}


async def test_goes_back_to_an_earlier_month(mock, session):
    reader = await ready_reader(session)
    await reader.go_to_month(2026, 12)
    await reader.go_to_month(2026, 10)
    assert await reader.displayed_month() == (2026, 10)


async def test_never_reads_the_old_calendar_during_a_reload(mock, session):
    mock.configure(loading_delay_ms=700)
    reader = await ready_reader(session)
    mock.set_work_orders([wo("2026-10-15", "AM", "available")])
    await reader.refresh()
    reading = await reader.read_displayed_month()
    assert reading.slots[S("2026-10-15 AM")].available == 1


async def test_blank_calendar_gets_exactly_one_reload(mock, session):
    reader = await ready_reader(session)
    mock.configure(blank_calendar_times=1)
    await reader.refresh()
    fetches = mock.stats()["calendar_fetches"]
    reading = await reader.read_displayed_month()
    assert len(reading.slots) == 31 * 2
    assert mock.stats()["calendar_fetches"] == fetches + 1


async def test_blank_twice_fails_without_more_retries(mock, session):
    reader = await ready_reader(session)
    mock.configure(blank_calendar_times=2)
    await reader.refresh()
    fetches = mock.stats()["calendar_fetches"]
    with pytest.raises(CalendarNotRendered):
        await reader.read_displayed_month()
    assert mock.stats()["calendar_fetches"] == fetches + 1


async def test_six_week_month(mock, session):
    mock.configure(today="2027-01-05")
    mock.set_work_orders([wo("2027-01-31", "AM", "available")])
    reader = await ready_reader(session)
    reading = await reader.read_displayed_month()
    assert (reading.year, reading.month) == (2027, 1)
    assert reading.slots[S("2027-01-31 AM")].available == 1
