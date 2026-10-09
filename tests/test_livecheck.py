from __future__ import annotations

import datetime as dt
import re

import pytest

from elisa_bot.livecheck import compare_with_site, run_site_check
from elisa_bot.models import Half, MonthReading, Slot, SlotTags
from elisa_bot.site.errors import InvalidCredentials
from elisa_bot.site.selectors import Selectors
from tests.conftest import fixed_now, wo

S = Slot.parse


def _status(word: str) -> str | None:
    for status, rx in Selectors().status_words.items():
        if re.match(rx, word.strip(), re.I):
            return status
    return None


def test_compare_matches_and_differences():
    reading = MonthReading(2026, 10, {Slot(dt.date(2026, 10, d), h): SlotTags() for d in range(1, 32) for h in Half})
    reading.slots[S("2026-10-08 AM")] = SlotTags(assigned=1)
    reading.slots[S("2026-10-09 PM")] = SlotTags(confirmed=2)
    schedule = {
        "2026-10-08": {"Assigned": {"am": 1, "pm": 0}},
        "2026-10-09": {"Confirmed": {"am": 0, "pm": 2}},
    }
    assert compare_with_site(reading, schedule, _status) == ([], set())

    schedule["2026-10-15"] = {"Assigned": {"am": 0, "pm": 1}, "Withdrawn": {"am": 1, "pm": 0}}
    problems, unknown = compare_with_site(reading, schedule, _status)
    assert problems == ["2026-10-15 PM: the site's data says 1 assigned, the calendar shows 0"]
    assert unknown == {"Withdrawn"}


@pytest.mark.browser
async def test_site_check_passes_when_the_reader_fits(mock, settings):
    mock.set_work_orders([
        wo("2026-10-08", "AM", "assigned"),
        wo("2026-10-08", "PM", "assigned"),
        wo("2026-10-09", "AM", "assigned"),
        wo("2026-10-09", "AM", "assigned"),
        wo("2026-10-05", "PM", "confirmed"),
        wo("2026-10-21", "AM", "applied"),
        wo("2026-10-27", "PM", "available"),
        wo("2026-11-03", "AM", "available"),
        wo("2026-11-16", "PM", "assigned"),
    ])
    report = await run_site_check(settings, Selectors(), now=fixed_now)
    names = [step.name for step in report.steps]
    assert names == [
        "Sign-in",
        "Open Work Orders tab",
        "Status filter buttons",
        "Calendar reload (Available)",
        "October 2026",
        "October 2026: cross-check with the site's data",
        "November 2026",
        "November 2026: cross-check with the site's data",
        "Sign-out",
    ]
    assert report.ok, report
    assert report.warnings == []
    assert "loading detected" in report.steps[3].detail
    assert report.steps[4].detail == "31 days; 1 available, 1 applied, 4 assigned, 1 confirmed"
    assert sorted(report.openings) == [S("2026-10-27 PM"), S("2026-11-03 AM")]
    stats = mock.stats()
    assert stats["logouts"] == 1 and stats["details_opened"] == 0 and stats["accept_calls"] == []


@pytest.mark.browser
async def test_site_check_lists_differences(mock, settings):
    mock.configure(schedule_drift=True)
    report = await run_site_check(settings, Selectors(), now=fixed_now)
    assert not report.ok
    assert report.mismatches == [
        "2026-10-20 AM: the site's data says 1 assigned, the calendar shows 0",
        "2026-11-20 AM: the site's data says 1 assigned, the calendar shows 0",
    ]


@pytest.mark.browser
async def test_site_check_flags_unknown_tag_words(mock, settings):
    mock.set_work_orders([wo("2026-10-14", "AM", "cancelled")])
    report = await run_site_check(settings, Selectors(), now=fixed_now)
    assert report.ok
    assert any("Cancelled" in warning for warning in report.warnings)


@pytest.mark.browser
async def test_site_check_reports_a_missing_piece(mock, settings):
    report = await run_site_check(settings, Selectors(month_title=".no-such-title"), now=fixed_now)
    assert not report.ok
    assert report.steps[-1].name in ("Open Work Orders tab", "Sign-out")
    failing = [step for step in report.steps if not step.ok]
    assert failing[0].name == "Open Work Orders tab"
    assert failing[0].detail == "navigation_failed: the calendar did not appear"
    assert report.diagnostics["month title (visible/all)"] == "0/0"
    assert report.diagnostics["status filter buttons (visible/all)"] == "4/4"
    assert report.diagnostics["'Open Work Orders' links (visible/all)"] == "1/2"
    assert report.diagnostics["element on top of the tab"] == "none (the tab is clickable)"
    assert report.http_errors == []


@pytest.mark.browser
async def test_site_check_stops_on_a_rejected_password(mock, settings):
    mock.configure(invalid_credentials=True)
    with pytest.raises(InvalidCredentials):
        await run_site_check(settings, Selectors(), now=fixed_now)
    assert mock.stats()["login_attempts"] == 1
