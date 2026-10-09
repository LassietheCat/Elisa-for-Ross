from __future__ import annotations

import logging

import pytest

from elisa_bot.cycle import run_check, run_once
from elisa_bot.models import ApplyOutcome, Slot
from elisa_bot.notify import Message
from elisa_bot.site.errors import InvalidCredentials
from elisa_bot.site.selectors import Selectors
from elisa_bot.state import StateStore
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


async def run(settings, capture):
    return await run_once(settings, Selectors(), notifier=capture, now=fixed_now)


async def test_check_reads_both_months_and_changes_nothing(mock, settings):
    mock.set_work_orders([wo("2026-10-14", "AM", "available"), wo("2026-11-03", "PM", "assigned")])
    slots = await run_check(settings, Selectors(), now=fixed_now)
    assert slots[S("2026-10-14 AM")].available == 1
    assert slots[S("2026-11-03 PM")].assigned == 1
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


async def test_skips_a_slot_where_the_owner_already_has_a_job(mock, tmp_path):
    settings = make_settings(tmp_path, mock.base_url, test_mode=False)
    capture = Capture()
    await run(settings, capture)
    mock.set_work_orders([wo("2026-10-15", "AM", "assigned"), wo("2026-10-15", "AM", "available")])
    report = await run(settings, capture)
    assert report.results == [] and report.skipped_booked == [S("2026-10-15 AM")]
    assert mock.stats()["accept_calls"] == [] and capture.messages == []


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


async def test_rejected_sign_in_is_recorded_and_not_retried(mock, settings):
    mock.configure(invalid_credentials=True)
    with pytest.raises(InvalidCredentials):
        await run(settings, Capture())
    assert mock.stats()["login_attempts"] == 1
    state = StateStore(settings.state_path).load()
    assert state.last_run_ok is False and state.last_error == "invalid_credentials"
