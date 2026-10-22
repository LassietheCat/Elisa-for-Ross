from __future__ import annotations

import datetime as dt
import logging
import time

import pytest

from elisa_bot.cycle import run_check, run_once
from elisa_bot.models import ApplyOutcome, Slot
from elisa_bot.notify import Message
from elisa_bot.site.errors import DataRequestFailed, InvalidCredentials, SiteBusy, SiteError
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SiteSession
from elisa_bot.state import ControlStore, StateStore
from tests.conftest import fixed_now, make_settings, wo
from tests.mock_site.server import MOCK_EMAIL, MOCK_PASSWORD

pytestmark = pytest.mark.browser
S = Slot.parse


class Capture:
    name = "capture"

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, message: Message) -> bool:
        self.messages.append(message)
        return True


async def run(settings, capture, session=None, month=None):
    return await run_once(settings, Selectors(), notifier=capture, now=fixed_now, session=session, month=month)


def november_first():
    return fixed_now().replace(month=11, day=1)


async def signed_in(settings) -> SiteSession:
    session = SiteSession(settings, Selectors())
    await session.start()
    await session.ensure_ready()
    return session


async def test_check_reads_three_months_and_changes_nothing(mock, settings):
    mock.set_work_orders([
        wo("2026-10-14", "AM", "available"),
        wo("2026-11-03", "PM", "assigned"),
        wo("2026-12-17", "AM", "available"),
    ])
    slots = await run_check(settings, Selectors(), now=fixed_now)
    assert len(slots) == (31 + 30 + 31) * 2
    assert slots[S("2026-10-14 AM")].available == 1
    assert slots[S("2026-11-03 PM")].assigned == 1
    assert slots[S("2026-12-17 AM")].available == 1
    assert not settings.state_path.exists()
    assert mock.stats()["details_opened"] == 0
    assert mock.stats()["logouts"] == 1


async def test_first_run_only_records_what_is_already_open(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    capture = Capture()
    report = await run(settings, capture)
    assert report.baseline_only and report.results == [] and capture.messages == []
    assert mock.stats()["details_opened"] == 0
    state = StateStore(settings.state_path).load()
    assert state.baseline_done and state.seen == ["2026-10-14 AM"]


async def test_a_month_that_comes_into_view_gets_a_first_look_too(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.configure(today="2026-11-01")
    mock.set_work_orders([wo("2027-01-12", "AM", "available")])
    report = await run_once(settings, Selectors(), notifier=capture, now=november_first)
    assert sorted(report.months_read) == [(2026, 11), (2026, 12), (2027, 1)]
    assert report.results == [] and capture.messages == [] and mock.stats()["accept_calls"] == []
    state = StateStore(settings.state_path).load()
    assert state.seen == ["2027-01-12 AM"] and state.watched_through == "2027-01" and state.baseline_done

    mock.set_work_orders([wo("2027-01-12", "AM", "available"), wo("2027-01-20", "PM", "available")])
    report = await run_once(settings, Selectors(), notifier=capture, now=november_first)
    assert [str(r.slot) for r in report.results] == ["2027-01-20 PM"]


async def test_test_mode_alerts_but_never_clicks(mock, settings):
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    report = await run(settings, capture)
    assert [r.outcome for r in report.results] == [ApplyOutcome.TEST_MODE]
    assert len(capture.messages) == 1
    assert capture.messages[0].subject == "Elisa: test mode, opening Wed Oct 14 AM"
    assert mock.stats()["accept_calls"] == []
    await run(settings, capture)
    assert len(capture.messages) == 1


async def test_live_run_accepts_both_months_and_sends_one_alert(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-11-03", "PM", "available")])
    report = await run(settings, capture)
    assert sorted(str(r.slot) for r in report.results if r.outcome is ApplyOutcome.APPLIED) == ["2026-10-14 AM", "2026-11-03 PM"]
    assert len(mock.stats()["accept_calls"]) == 2
    assert capture.messages == [Message("Elisa: applied Wed Oct 14 AM (+1 more)", "Applied: Wed Oct 14 AM, Tue Nov 3 PM.")]
    report = await run(settings, capture)
    assert report.results == [] and len(capture.messages) == 1


async def test_opening_two_months_ahead_is_found(mock, settings):
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-12-10", "AM", "available")])
    report = await run(settings, capture)
    assert [(str(r.slot), r.outcome) for r in report.results] == [("2026-12-10 AM", ApplyOutcome.TEST_MODE)]
    assert capture.messages[0].subject == "Elisa: test mode, opening Thu Dec 10 AM"


async def test_skips_a_slot_where_the_owner_already_has_a_job_and_says_so(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-15", "AM", "assigned"), wo("2026-10-15", "AM", "available")])
    report = await run(settings, capture)
    assert report.results == [] and report.skipped_booked == [S("2026-10-15 AM")]
    assert mock.stats()["accept_calls"] == []
    assert capture.messages == [
        Message("Elisa: opening Thu Oct 15 AM (already booked)", "Skipped, you already have a job then: Thu Oct 15 AM.")
    ]


async def test_skipped_alert_can_be_turned_off(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False, notify_skipped=False)
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-15", "AM", "assigned"), wo("2026-10-15", "AM", "available")])
    report = await run(settings, capture)
    assert report.skipped_booked == [S("2026-10-15 AM")] and capture.messages == []


async def test_nothing_private_is_stored_logged_or_sent(mock, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    await run(settings, capture)
    stored = settings.state_path.read_text()
    sent = " ".join(m.subject + " " + m.text for m in capture.messages)
    assert sent
    for text in (stored, sent, caplog.text):
        assert "CONFIDENTIAL" not in text
        assert "WO 999" not in text
        assert MOCK_PASSWORD not in text
        assert MOCK_EMAIL not in text


async def test_calendar_tags_the_bot_cannot_read_are_logged_and_emailed(mock, settings, caplog):
    caplog.set_level(logging.WARNING)
    mock.set_work_orders([wo("2026-10-21", "PM", "cancelled")])
    capture = Capture()
    await run(settings, capture)
    assert "2026-10: calendar tags with labels the bot doesn't know: Cancelled" in caplog.text
    assert "2026-10-21: 1 calendar tag(s) the bot couldn't read" in caplog.text
    assert capture.messages == [Message(
        "Elisa: the bot may be missing openings",
        "October 2026: calendar tags with labels the bot doesn't know (Cancelled). "
        "Wed Oct 21: 1 calendar tag(s) the bot couldn't read. "
        "Please check Open Work Orders in Elisa by hand. The bot keeps checking.",
    )]


async def test_more_openings_in_elisas_own_count_are_logged_and_emailed_once(mock, settings, caplog):
    caplog.set_level(logging.WARNING)
    mock.configure(count_extra_new=1)
    capture = Capture()
    session = await signed_in(settings)
    try:
        await run(settings, capture, session)
        await run(settings, capture, session, month=(2026, 10))
    finally:
        await session.close()
    warning = "2026-10: Elisa's own count shows 1 open work order(s), the calendar shows 0"
    assert caplog.text.count(warning) == 1
    assert "2026-11: Elisa's own count shows 1 open work order(s)" in caplog.text
    assert [m.subject for m in capture.messages] == ["Elisa: the bot may be missing openings"]
    assert "October 2026: Elisa lists 1 open work order(s), but the bot saw 0 on the calendar." in capture.messages[0].text
    assert "December 2026: Elisa lists 1 open work order(s)" in capture.messages[0].text


async def test_matching_counts_and_known_tags_log_and_send_nothing(mock, settings, caplog):
    caplog.set_level(logging.INFO)
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-10-15", "PM", "assigned")])
    capture = Capture()
    await run(settings, capture)
    assert caplog.text.count("Reading Elisa's own open-work-order count too") == 1
    assert "couldn't read" not in caplog.text and "own count shows" not in caplog.text
    assert capture.messages == []


async def test_an_opening_without_an_accept_button_is_reported_and_tried_once_more(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.configure(apply_mode="no_button")
    mock.set_work_orders([wo("2026-10-14", "AM", "available")])
    report = await run(settings, capture)
    assert [(r.outcome, r.code) for r in report.results] == [(ApplyOutcome.ERROR, "no_accept_button")]
    assert capture.messages[-1].text == "Opening seen but Accept could not be tried, will retry: Wed Oct 14 AM."
    report = await run(settings, capture)
    assert [r.outcome for r in report.results] == [ApplyOutcome.ERROR]
    report = await run(settings, capture)
    assert report.results == [] and mock.stats()["accept_calls"] == []


async def test_rejected_sign_in_is_recorded_and_not_retried(mock, settings):
    mock.configure(invalid_credentials=True)
    with pytest.raises(InvalidCredentials):
        await run(settings, Capture())
    assert mock.stats()["login_attempts"] == 1
    state = StateStore(settings.state_path).load()
    assert state.last_run_ok is False and state.last_error == "invalid_credentials"


async def test_rate_limited_check_changes_nothing_and_accepts_nothing(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    session = await signed_in(settings)
    try:
        await run(settings, capture, session)
        mock.set_work_orders([wo("2026-10-14", "AM", "available")])
        mock.configure(rate_limited_times=1, retry_after="7")
        with pytest.raises(SiteBusy) as info:
            await run(settings, capture, session)
        assert info.value.retry_after == 7
        state = StateStore(settings.state_path).load()
        assert state.last_run_ok is False and state.last_error == "site_busy"
        assert state.seen == []
        assert mock.stats()["details_opened"] == 0 and mock.stats()["accept_calls"] == []
        assert capture.messages == []

        report = await run(settings, capture, session)
        assert [r.outcome for r in report.results] == [ApplyOutcome.APPLIED]
    finally:
        await session.close()


async def test_rate_limited_calendar_is_reported_as_busy(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    session = await signed_in(settings)
    try:
        await run(settings, Capture(), session)
        mock.configure(rate_limited_times=3)
        with pytest.raises(SiteBusy) as info:
            await run(settings, Capture(), session)
        assert isinstance(info.value.__cause__, SiteError)
        assert StateStore(settings.state_path).load().last_error == "site_busy"
    finally:
        await session.close()


async def test_failed_calendar_data_request_fails_the_check(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    session = await signed_in(settings)
    try:
        await run(settings, Capture(), session)
        mock.set_work_orders([wo("2026-10-14", "AM", "available")])
        mock.configure(schedule_error_times=1)
        with pytest.raises(DataRequestFailed):
            await run(settings, Capture(), session)
        assert StateStore(settings.state_path).load().last_error == "data_request_failed"
        assert mock.stats()["details_opened"] == 0 and mock.stats()["accept_calls"] == []
    finally:
        await session.close()


async def test_an_accept_is_still_reported_when_the_other_month_fails(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    session = await signed_in(settings)
    try:
        await run(settings, capture, session)
        openings = {"2026-10-14 AM", "2026-11-03 PM", "2026-12-03 PM"}
        mock.set_work_orders([wo(*slot.split(" "), "available") for slot in sorted(openings)])
        mock.configure(schedule_error_times=1, schedule_error_after=1)
        with pytest.raises(DataRequestFailed):
            await run(settings, capture, session)
        applied = list(StateStore(settings.state_path).load().applied)
        assert len(applied) == 1 and len(mock.stats()["accept_calls"]) == 1
        label = S(applied[0]).label()
        assert capture.messages == [Message(f"Elisa: applied {label}", f"Applied: {label}.")]

        report = await run(settings, capture, session)
        assert sorted(str(r.slot) for r in report.results) == sorted(openings - set(applied))
        assert len(capture.messages) == 2
    finally:
        await session.close()


async def test_single_month_checks_load_once_and_keep_the_other_months_memory(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url)
    capture = Capture()
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-11-03", "PM", "available")])
    session = await signed_in(settings)
    store = StateStore(settings.state_path)
    try:
        report = await run(settings, capture, session, month=(2026, 10))
        assert report.months_read == [(2026, 10)] and report.baseline_only
        state = store.load()
        assert state.seen == ["2026-10-14 AM"] and state.baseline_pending == ["2026-11", "2026-12"]
        assert not state.baseline_done

        await run(settings, capture, session, month=(2026, 11))
        await run(settings, capture, session, month=(2026, 12))
        state = store.load()
        assert state.seen == ["2026-10-14 AM", "2026-11-03 PM"] and state.baseline_done

        report = await run(settings, capture, session, month=(2026, 11))
        assert report.results == [] and capture.messages == []
        assert mock.stats()["open_list_loads"] == ["2026-10", "2026-10", "2026-11", "2026-12", "2026-11"]

        mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-11-03", "PM", "available"),
                              wo("2026-11-24", "AM", "available")])
        report = await run(settings, capture, session, month=(2026, 11))
        assert [str(r.slot) for r in report.results] == ["2026-11-24 AM"]
        assert store.load().seen == ["2026-10-14 AM", "2026-11-03 PM", "2026-11-24 AM"]
    finally:
        await session.close()


async def test_a_month_the_page_already_loaded_is_reloaded_before_it_is_read(mock, settings):
    session = await signed_in(settings)
    try:
        await run(settings, Capture(), session, month=(2026, 10))
        await run(settings, Capture(), session, month=(2026, 11))
        loads = len(mock.stats()["open_list_loads"])
        mock.set_work_orders([wo("2026-10-21", "PM", "available")])
        report = await run(settings, Capture(), session, month=(2026, 10))
        assert report.available == {S("2026-10-21 PM")}
        assert mock.stats()["open_list_loads"][loads:] == ["2026-10"]
    finally:
        await session.close()


async def test_a_quick_check_hit_reads_only_the_months_with_openings_and_times_the_result(mock, settings):
    session = await signed_in(settings)
    try:
        await run(settings, Capture(), session)
        mock.set_work_orders([wo("2026-12-08", "PM", "available")])
        loads = len(mock.stats()["calendar_months"])
        report = await run_once(
            settings, Selectors(), notifier=Capture(), now=fixed_now, session=session,
            only=[(2026, 12)], detected_at=time.monotonic(),
        )
        assert report.months_read == [(2026, 12)]
        assert "2026-10" not in mock.stats()["calendar_months"][loads:]
        entry = StateStore(settings.state_path).load().activity[-1]
        assert (entry["slot"], entry["outcome"]) == ("2026-12-08 PM", "test_mode")
        assert isinstance(entry["seconds"], float) and entry["seconds"] < 60
    finally:
        await session.close()


async def test_an_opening_gone_before_the_read_is_recorded_as_gone(mock, settings):
    session = await signed_in(settings)
    try:
        await run(settings, Capture(), session)
        report = await run_once(
            settings, Selectors(), notifier=Capture(), now=fixed_now, session=session,
            only=[(2026, 11)], detected_at=time.monotonic(),
        )
        assert report.results == []
        entry = StateStore(settings.state_path).load().activity[-1]
        assert (entry["slot"], entry["outcome"]) == (None, "gone")
    finally:
        await session.close()


async def test_new_gone_and_changed_jobs_on_the_calendar_are_logged_by_date_and_half_day(mock, settings, caplog):
    caplog.set_level(logging.INFO)
    mock.set_work_orders([wo("2026-10-21", "PM", "assigned"), wo("2026-11-04", "AM", "assigned")])
    await run(settings, Capture())
    assert "2026-10: watching 1 booked half-day(s) for new jobs" in caplog.text
    mock.set_work_orders([
        wo("2026-10-21", "PM", "confirmed"),
        wo("2026-10-22", "AM", "applied"),
        wo("2026-10-23", "PM", "assigned"),
        wo("2026-10-23", "PM", "assigned"),
    ])
    await run(settings, Capture())
    assert [r.getMessage() for r in caplog.records if "on the calendar:" in r.getMessage()] == [
        "Job changed on the calendar: Wed Oct 21 PM, now 1 Confirmed (was 1 Assigned at 10:00:00)",
        "New job on the calendar: Thu Oct 22 AM, now 1 Applied (was none at 10:00:00)",
        "New job on the calendar: Fri Oct 23 PM, now 2 Assigned (was none at 10:00:00)",
        "Job gone on the calendar: Wed Nov 4 AM, now none (was 1 Assigned at 10:00:00)",
    ]
    jobs = StateStore(settings.state_path).load().jobs
    assert jobs["2026-10"]["slots"] == {
        "2026-10-21 PM": {"confirmed": 1},
        "2026-10-22 AM": {"applied": 1},
        "2026-10-23 PM": {"assigned": 2},
    }
    assert jobs["2026-11"]["slots"] == {} and jobs["2026-12"]["read_at"] == "2026-10-07T10:00:00-04:00"


async def test_an_opening_on_a_day_off_is_not_accepted(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    ControlStore(settings).update(by="test", now=fixed_now(), add_day_off=dt.date(2026, 10, 14))
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-10-15", "PM", "available")])
    report = await run(settings, capture)
    assert [str(r.slot) for r in report.results] == ["2026-10-15 PM"] and report.skipped_day_off == [S("2026-10-14 AM")]
    assert len(mock.stats()["accept_calls"]) == 1
    assert capture.messages[-1].text == "Applied: Thu Oct 15 PM. Not accepted, marked as a day off: Wed Oct 14 AM."
    state = StateStore(settings.state_path).load()
    assert [(a["slot"], a["outcome"]) for a in state.activity] == [("2026-10-14 AM", "day off"), ("2026-10-15 PM", "applied")]
    assert state.daily[fixed_now().date().isoformat()]["reads"] == 2
