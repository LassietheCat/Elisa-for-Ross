from __future__ import annotations

import asyncio
import dataclasses
from typing import Callable

import pytest

from elisa_bot.notify import Message
from elisa_bot.service import next_delay, run_forever
from elisa_bot.site.selectors import Selectors
from elisa_bot.state import ControlStore, StateStore
from tests.conftest import fixed_now, make_settings, wo


class Capture:
    name = "capture"

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, message: Message) -> bool:
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
