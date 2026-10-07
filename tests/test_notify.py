from __future__ import annotations

import logging
import smtplib

import pytest

from elisa_bot import notify
from elisa_bot.config import Settings
from elisa_bot.models import ApplyOutcome, ApplyResult, Slot
from elisa_bot.notify import EmailNotifier, Message, cycle_message

S = Slot.parse


def result(slot: str, outcome: ApplyOutcome) -> ApplyResult:
    return ApplyResult(S(slot), outcome, clicked=outcome in (ApplyOutcome.APPLIED, ApplyOutcome.UNVERIFIED, ApplyOutcome.MISSED))


def test_no_message_when_nothing_happened():
    assert cycle_message([]) is None


def test_applied_message_holds_only_date_and_half():
    message = cycle_message([result("2026-10-14 AM", ApplyOutcome.APPLIED)])
    assert message == Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM.")


def test_one_message_covers_the_whole_run():
    message = cycle_message(
        [
            result("2026-10-15 PM", ApplyOutcome.APPLIED),
            result("2026-10-14 AM", ApplyOutcome.APPLIED),
            result("2026-10-20 AM", ApplyOutcome.MISSED),
        ],
        skipped=[S("2026-10-22 PM")],
    )
    assert message.subject == "Elisa: applied Wed Oct 14 AM (+1 more)"
    assert message.text == (
        "Applied: Wed Oct 14 AM, Thu Oct 15 PM. "
        "Missed, taken before Accept finished: Tue Oct 20 AM. "
        "Skipped, you already have a job then: Thu Oct 22 PM."
    )


def test_test_mode_wording():
    message = cycle_message([result("2026-10-14 AM", ApplyOutcome.TEST_MODE)])
    assert message.subject == "Elisa: test mode, opening Wed Oct 14 AM"
    assert "did not click" in message.text


def test_missed_can_be_left_out():
    assert cycle_message([result("2026-10-20 AM", ApplyOutcome.MISSED)], include_missed=False) is None


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail = False

    def __init__(self, host, port, timeout=None, **kwargs):
        self.host, self.port, self.calls, self.sent = host, port, [], None
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        if FakeSMTP.fail:
            raise smtplib.SMTPServerDisconnected("gone")
        self.sent = message
        self.calls.append("send")

    def quit(self):
        self.calls.append("quit")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail = False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def email_settings() -> Settings:
    return Settings(
        smtp_host="smtp.example.test",
        smtp_username="alerts@example.test",
        smtp_password="smtp-secret-value",
        smtp_from="alerts@example.test",
        alert_email_to=("owner@example.test", "second@example.test"),
    )


async def test_email_is_sent_over_starttls(fake_smtp, caplog):
    caplog.set_level(logging.DEBUG)
    sent = await EmailNotifier(email_settings()).send(Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM."))
    assert sent
    smtp = fake_smtp.instances[-1]
    assert smtp.calls == ["starttls", ("login", "alerts@example.test"), "send", "quit"]
    assert smtp.sent["To"] == "owner@example.test, second@example.test"
    assert smtp.sent["Subject"] == "Elisa: applied Wed Oct 14 AM"
    assert smtp.sent.get_content().strip() == "Applied: Wed Oct 14 AM."
    assert "smtp-secret-value" not in caplog.text


async def test_email_failure_retries_then_gives_up(fake_smtp):
    fake_smtp.fail = True
    sent = await EmailNotifier(email_settings(), retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert not sent
    assert len(fake_smtp.instances) == 3


def test_console_is_used_until_email_is_configured():
    assert isinstance(notify.build_notifier(Settings()), notify.ConsoleNotifier)
    assert isinstance(notify.build_notifier(email_settings()), notify.EmailNotifier)
