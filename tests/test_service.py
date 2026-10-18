from __future__ import annotations

import asyncio
import dataclasses
import logging
import datetime as dt
from email.utils import format_datetime
from typing import Callable

import pytest
from playwright.async_api import Error as PlaywrightError

from elisa_bot import service
from elisa_bot.cycle import CycleReport
from elisa_bot.notify import Message
from elisa_bot.service import FailureAlerts, MonthRotation, QuickCheck, next_delay, run_forever
from elisa_bot.site.errors import CalendarNotRendered, DataRequestFailed, SignInPaused, SiteBusy
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SignInLimiter, parse_retry_after
from elisa_bot.state import BotState, ControlStore, StateStore
from tests.conftest import fixed_now, make_settings, wo


class Capture:
    name = "capture"

    def __init__(self, *, fail_first: int = 0) -> None:
        self.messages: list[Message] = []
        self.fail_first = fail_first

    async def send(self, message: Message) -> bool:
        if self.fail_first > 0:
            self.fail_first -= 1
            return False
        self.messages.append(message)
        return True


async def until(predicate: Callable[[], bool], timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.2)


def fast(settings):
    return dataclasses.replace(
        settings, poll_seconds=1, min_poll_seconds=1, quiet_after_sign_in_seconds=0, quick_check_seconds=0
    )


def start(settings, capture):
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(settings, Selectors(), stop=stop, notifier=capture, now=fixed_now))
    return stop, task


def test_next_delay_backs_off_and_caps():
    assert [next_delay(n, 15) for n in range(7)] == [15, 30, 60, 120, 240, 300, 300]


def test_next_delay_waits_longer_when_the_site_is_busy():
    assert [next_delay(n, 15, busy=True) for n in range(7)] == [15, 60, 120, 240, 480, 900, 900]
    assert next_delay(1, 15, busy=True, retry_after=600) == 600
    assert next_delay(1, 15, busy=True, retry_after=99_999) == 3600


def test_sign_in_limiter_allows_a_few_per_hour():
    now = [0.0]
    limiter = SignInLimiter(2, clock=lambda: now[0])
    limiter.take()
    now[0] = 10
    limiter.take()
    now[0] = 20
    with pytest.raises(SignInPaused) as info:
        limiter.take()
    assert info.value.retry_after == 3580
    now[0] = 3600
    limiter.take()


def test_retry_after_reads_seconds_and_dates():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90)
    assert 85 <= parse_retry_after(format_datetime(later, usegmt=True)) <= 90


async def test_one_alert_after_three_failures_then_one_when_working_again():
    capture = Capture()
    alerts = FailureAlerts(capture)
    for _ in range(2):
        await alerts.failed("site_busy")
    assert capture.messages == []
    for _ in range(3):
        await alerts.failed("site_busy")
    assert [m.subject for m in capture.messages] == ["Elisa: bot can't check right now"]
    await alerts.succeeded()
    await alerts.succeeded()
    assert [m.subject for m in capture.messages] == ["Elisa: bot can't check right now", "Elisa: bot is working again"]
    assert capture.messages[1].text == "Checks are working again after 5 failed tries."
    assert alerts.failures == 0


async def test_failure_alert_is_tried_again_if_the_email_did_not_go_out():
    capture = Capture(fail_first=1)
    alerts = FailureAlerts(capture)
    for _ in range(3):
        await alerts.failed("calendar_not_rendered")
    assert capture.messages == []
    await alerts.failed("calendar_not_rendered")
    assert len(capture.messages) == 1
    assert capture.messages[0].text.startswith("The last 4 checks failed.")


async def test_no_working_again_email_without_a_failure_alert():
    capture = Capture()
    alerts = FailureAlerts(capture)
    await alerts.failed("site_busy")
    await alerts.succeeded()
    assert capture.messages == []


class BrokenBrowserSession:
    def __init__(self, *args, **kwargs) -> None:
        self.started = False

    async def start(self) -> None:
        raise PlaywrightError("Executable doesn't exist")

    async def logout(self) -> bool:
        return False

    async def close(self) -> None:
        pass


async def test_a_browser_that_will_not_start_does_not_stop_the_bot(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "SiteSession", BrokenBrowserSession)
    settings = fast(make_settings(tmp_path, "http://127.0.0.1:9"))
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await until(lambda: len(capture.messages) == 1)
        assert "web browser had a problem" in capture.messages[0].text
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


class IdlePage:
    def __init__(self) -> None:
        self.reloads = 0

    async def goto(self, *args, **kwargs) -> None:
        self.reloads += 1


class IdleSession(BrokenBrowserSession):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        self.page = IdlePage()

    async def start(self) -> None:
        self.started = True


async def after_a_slow_check(monkeypatch, settings, finish) -> tuple[float, int]:
    loop = asyncio.get_running_loop()
    starts: list[float] = []
    ends: list[float] = []
    pages: set[IdlePage] = set()

    async def slow_check(*args, **kwargs):
        pages.add(kwargs["session"].page)
        starts.append(loop.time())
        await asyncio.sleep(1)
        ends.append(loop.time())
        return finish()

    monkeypatch.setattr(service, "SiteSession", IdleSession)
    monkeypatch.setattr(service, "run_once", slow_check)
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    stop, task = start(settings, Capture())
    try:
        await until(lambda: len(starts) == 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
    return starts[1] - ends[0], sum(page.reloads for page in pages)


def refused():
    raise SiteBusy("the site answered 429 Too Many Requests")


def page_broke():
    raise CalendarNotRendered("timed out waiting for the calendar to finish loading")


def data_failed():
    raise DataRequestFailed("a calendar data request failed (500)")


def quiet(tmp_path, seconds: int = 3):
    return dataclasses.replace(fast(make_settings(tmp_path, "http://127.0.0.1:9")), quiet_after_sign_in_seconds=seconds)


async def test_a_busy_site_gets_the_whole_wait_after_a_slow_check(monkeypatch, tmp_path):
    gap, reloads = await after_a_slow_check(monkeypatch, fast(make_settings(tmp_path, "http://127.0.0.1:9")), refused)
    assert gap >= next_delay(1, 1, busy=True) and reloads == 0


async def test_the_quiet_period_starts_when_the_sign_in_check_ends(monkeypatch, tmp_path):
    gap, _ = await after_a_slow_check(monkeypatch, quiet(tmp_path), lambda: CycleReport(signed_in=True))
    assert gap >= 3


async def test_reloading_the_page_after_a_failed_check_is_followed_by_the_quiet_period(monkeypatch, tmp_path):
    gap, reloads = await after_a_slow_check(monkeypatch, quiet(tmp_path), page_broke)
    assert gap >= 3 and reloads >= 1


async def test_a_failed_data_request_is_retried_without_reloading_the_page(monkeypatch, tmp_path):
    _, reloads = await after_a_slow_check(monkeypatch, quiet(tmp_path), data_failed)
    assert reloads == 0


@pytest.mark.browser
async def test_switch_controls_the_always_on_bot(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    controls = ControlStore(settings)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await asyncio.sleep(2)
        assert mock.stats()["login_attempts"] == 0

        controls.update(by="test", now=fixed_now(), enabled=True)
        await until(lambda: StateStore(settings.state_path).load().baseline_done)
        mock.set_work_orders([wo("2026-10-14", "AM", "available")])
        await until(lambda: len(capture.messages) == 1)
        assert capture.messages[0].subject == "Elisa: test mode, opening Wed Oct 14 AM"

        controls.update(by="test", now=fixed_now(), enabled=False)
        await until(lambda: mock.stats()["logouts"] == 1)
        stats = mock.stats()
        assert stats["logins_ok"] == 1
        assert stats["accept_calls"] == []
        await asyncio.sleep(2)
        assert mock.stats()["calendar_fetches"] == stats["calendar_fetches"]
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


@pytest.mark.browser
async def test_rejected_password_switches_the_bot_off(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    mock.configure(invalid_credentials=True)
    controls = ControlStore(settings)
    controls.update(by="test", now=fixed_now(), enabled=True)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await until(lambda: not controls.load().enabled)
        await until(lambda: len(capture.messages) == 1)
        assert capture.messages[0].subject == "Elisa: bot switched off (sign-in problem)"
        assert controls.load().updated_by == "bot"
        await asyncio.sleep(2)
        assert mock.stats()["login_attempts"] == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


@pytest.mark.browser
async def test_failed_check_backs_off_then_recovers_without_signing_in_again(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    mock.configure(blank_calendar_times=3)
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    stop, task = start(settings, Capture())
    try:
        await until(lambda: StateStore(settings.state_path).load().last_run_ok is False)
        assert StateStore(settings.state_path).load().last_error == "calendar_not_rendered"
        await until(lambda: StateStore(settings.state_path).load().last_run_ok is True)
        assert mock.stats()["logins_ok"] == 1
        assert mock.stats()["dashboard_loads"] > 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


@pytest.mark.browser
async def test_a_failed_data_request_is_retried_on_the_same_page(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    store = StateStore(settings.state_path)
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    stop, task = start(settings, Capture())
    try:
        await until(lambda: store.load().baseline_done)
        mock.configure(schedule_error_times=1)
        await until(lambda: store.load().last_error == "data_request_failed")
        await until(lambda: store.load().last_run_ok is True)
        assert mock.stats()["dashboard_loads"] == 1 and mock.stats()["logins_ok"] == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


@pytest.mark.browser
async def test_forced_failures_send_exactly_one_alert_then_recover(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    mock.configure(blank_calendar_times=1000)
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await until(lambda: len(capture.messages) == 1)
        assert capture.messages[0].subject == "Elisa: bot can't check right now"
        assert "calendar did not load" in capture.messages[0].text
        fetches = mock.stats()["calendar_fetches"]
        await until(lambda: mock.stats()["calendar_fetches"] > fetches)
        await asyncio.sleep(4)
        assert len(capture.messages) == 1

        mock.configure(blank_calendar_times=0)
        await until(lambda: len(capture.messages) == 2)
        assert capture.messages[1].subject == "Elisa: bot is working again"
        assert StateStore(settings.state_path).load().last_run_ok is True
        assert mock.stats()["logins_ok"] == 1
        assert mock.stats()["accept_calls"] == []
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


def test_month_rotation_moves_one_month_at_a_time():
    months = [(2026, 10), (2026, 11), (2026, 12)]
    rotation = MonthRotation()
    assert [rotation.next(months) for _ in range(8)] == [(2026, 10), (2026, 11), (2026, 12), (2026, 11)] * 2
    single = MonthRotation()
    assert [single.next([(2026, 10)]) for _ in range(3)] == [(2026, 10)] * 3


@pytest.mark.browser
async def test_always_on_bot_loads_one_month_per_check(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    stop, task = start(settings, Capture())
    try:
        await until(lambda: len(mock.stats()["calendar_months"]) >= 7)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
    assert mock.stats()["calendar_months"][:7] == [
        "2026-10", "2026-11", "2026-12", "2026-11", "2026-10", "2026-11", "2026-12",
    ]
    assert mock.stats()["logins_ok"] == 1


@pytest.mark.browser
async def test_openings_already_listed_in_any_month_are_only_recorded(mock, tmp_path):
    settings = fast(make_settings(tmp_path, mock.base_url))
    already = [wo("2026-11-05", "AM", "available"), wo("2026-12-08", "PM", "available")]
    mock.set_work_orders(already)
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await until(lambda: StateStore(settings.state_path).load().baseline_done)
        assert capture.messages == []
        assert StateStore(settings.state_path).load().seen == ["2026-11-05 AM", "2026-12-08 PM"]
        mock.set_work_orders([*already, wo("2026-10-21", "PM", "available")])
        await until(lambda: len(capture.messages) == 1)
        assert capture.messages[0].subject == "Elisa: test mode, opening Wed Oct 21 PM"
        loads = len(mock.stats()["calendar_months"])
        await until(lambda: len(mock.stats()["calendar_months"]) >= loads + 4)
        assert len(capture.messages) == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


class CountingSession:
    def __init__(self, *counts):
        self.counts = list(counts)
        self.ranges = []

    async def count_open(self, start, end, today):
        self.ranges.append((start, end, today))
        value = self.counts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


async def test_quick_check_reads_the_calendar_only_when_elisas_count_goes_up(tmp_path):
    quick = QuickCheck(make_settings(tmp_path, "http://127.0.0.1:9"), fixed_now)
    session = CountingSession(0, 0, 1, 1, None, 0, 2)
    assert [await quick.check(session) for _ in range(7)] == [False, False, True, False, False, False, True]
    assert session.ranges[0] == (dt.date(2026, 10, 1), dt.date(2026, 12, 31), dt.date(2026, 10, 7))


async def test_a_refused_quick_check_pauses_until_the_next_calendar_read(tmp_path):
    quick = QuickCheck(make_settings(tmp_path, "http://127.0.0.1:9"), fixed_now)
    session = CountingSession(SiteBusy("the site answered 429 Too Many Requests"), 0, 1)
    assert await quick.check(session) is False and quick.paused
    assert await quick.check(session) is False and len(session.ranges) == 1
    quick.calendar_read()
    assert await quick.check(session) is False and await quick.check(session) is True


async def test_refusals_slow_quick_checks_down_until_a_calm_half_hour(tmp_path):
    now = [0.0]
    quick = QuickCheck(make_settings(tmp_path, "http://127.0.0.1:9"), fixed_now, timer=lambda: now[0])
    busy = SiteBusy("the site answered 429 Too Many Requests")
    session = CountingSession(busy, busy, busy, busy, 0, 0)
    everies = []
    for _ in range(4):
        await quick.check(session)
        quick.calendar_read()
        everies.append(quick.every)
    assert everies == [40, 80, 120, 120]
    now[0] = 1799
    await quick.check(session)
    assert quick.every == 120
    now[0] = 1800
    await quick.check(session)
    assert quick.every == 20


@pytest.mark.browser
async def test_a_new_opening_is_found_by_the_quick_check_long_before_the_next_calendar_read(mock, tmp_path):
    settings = dataclasses.replace(fast(make_settings(tmp_path, mock.base_url)), poll_seconds=30, quick_check_seconds=1)
    StateStore(settings.state_path).save(BotState(baseline_done=True, watched_through="2026-12"))
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    capture = Capture()
    loop = asyncio.get_running_loop()
    stop, task = start(settings, capture)
    try:
        await until(lambda: len(mock.stats()["calendar_months"]) >= 1)
        await asyncio.sleep(3)
        reads = len(mock.stats()["calendar_months"])
        mock.set_work_orders([wo("2026-11-03", "PM", "available")])
        added = loop.time()
        await until(lambda: len(capture.messages) == 1, timeout=25)
        assert loop.time() - added < 15
        assert capture.messages[0].subject == "Elisa: test mode, opening Tue Nov 3 PM"
        assert len(mock.stats()["calendar_months"]) >= reads + 2
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


@pytest.mark.browser
async def test_quick_checks_keep_going_while_a_failed_calendar_read_waits(mock, tmp_path):
    settings = dataclasses.replace(fast(make_settings(tmp_path, mock.base_url)), poll_seconds=30, quick_check_seconds=1)
    StateStore(settings.state_path).save(BotState(baseline_done=True, watched_through="2026-12"))
    ControlStore(settings).update(by="test", now=fixed_now(), enabled=True)
    mock.configure(schedule_error_times=1)
    capture = Capture()
    stop, task = start(settings, capture)
    try:
        await until(lambda: StateStore(settings.state_path).load().last_error == "data_request_failed")
        await asyncio.sleep(3)
        mock.set_work_orders([wo("2026-10-21", "AM", "available")])
        await until(lambda: len(capture.messages) == 1, timeout=25)
        assert capture.messages[0].subject == "Elisa: test mode, opening Wed Oct 21 AM"
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


async def test_quick_checks_say_once_when_they_start_and_once_when_they_get_no_answer(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    quick = QuickCheck(make_settings(tmp_path, "http://127.0.0.1:9"), fixed_now)
    session = CountingSession(*([None] * 31), 0, 0)
    for _ in range(33):
        await quick.check(session)
    assert caplog.text.count("Quick checks got no answer from Elisa for 300 s") == 1
    assert caplog.text.count("Quick checks running: Elisa lists 0 open work order(s)") == 1
