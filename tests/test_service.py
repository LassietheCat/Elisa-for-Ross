from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from email.utils import format_datetime
from typing import Callable

import pytest
from playwright.async_api import Error as PlaywrightError

from elisa_bot import service
from elisa_bot.notify import Message
from elisa_bot.service import FailureAlerts, next_delay, run_forever
from elisa_bot.site.errors import SignInPaused
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SignInLimiter, parse_retry_after
from elisa_bot.state import ControlStore, StateStore
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
    return dataclasses.replace(settings, poll_seconds=1, min_poll_seconds=1)


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
