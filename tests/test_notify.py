from __future__ import annotations

import logging
import smtplib
import socket

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from elisa_bot import notify
from elisa_bot.config import Settings
from elisa_bot.models import ApplyOutcome, ApplyResult, Slot
from elisa_bot.notify import (
    EmailNotifier,
    Message,
    cycle_message,
    paused_message,
    recovered_message,
    trouble_message,
)

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


def test_paused_message_explains_what_to_do():
    message = paused_message("invalid_credentials")
    assert message.subject == "Elisa: bot switched off (sign-in problem)"
    assert "invalid_credentials" in message.text and "switch the bot back on" in message.text


OWNER = "owner@example.test"
APP_PASSWORD = "app-password-1234"


class Inbox:
    def __init__(self) -> None:
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK"


def _check_login(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == OWNER.encode() and auth_data.password == APP_PASSWORD.encode()
    return AuthResult(success=ok)


@pytest.fixture
def mail_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    inbox = Inbox()
    controller = Controller(inbox, hostname="127.0.0.1", port=port, authenticator=_check_login, auth_require_tls=False)
    controller.start()
    yield inbox, port
    controller.stop()


def owner_account(port: int, password: str = APP_PASSWORD) -> Settings:
    return Settings.from_env({
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(port),
        "SMTP_STARTTLS": "false",
        "SMTP_USERNAME": OWNER,
        "SMTP_PASSWORD": password,
        "ALERT_EMAIL_TO": OWNER,
    })


async def test_alert_goes_out_from_the_owners_own_account(mail_server):
    inbox, port = mail_server
    sent = await EmailNotifier(owner_account(port)).send(Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM."))
    assert sent
    envelope = inbox.envelopes[-1]
    assert envelope.mail_from == OWNER and envelope.rcpt_tos == [OWNER]
    content = envelope.content.decode()
    assert f"From: Elisa Bot <{OWNER}>" in content
    assert "Subject: Elisa: applied Wed Oct 14 AM" in content
    assert "Applied: Wed Oct 14 AM." in content


async def test_wrong_app_password_is_reported_not_raised(mail_server, caplog):
    inbox, port = mail_server
    sent = await EmailNotifier(owner_account(port, "wrong"), retries=0).send(Message("s", "t"))
    assert sent is False
    assert inbox.envelopes == []
    assert "wrong" not in caplog.text


def test_trouble_alert_says_why_and_holds_no_job_details():
    message = trouble_message(3, "site_busy")
    assert message.subject == "Elisa: bot can't check right now"
    assert message.text.startswith("The last 3 checks failed. Elisa asked the bot to slow down (too many requests).")
    assert "Unexpected problem (odd_code)." in trouble_message(3, "odd_code").text
    assert recovered_message(4) == Message("Elisa: bot is working again", "Checks are working again after 4 failed tries.")


def test_an_opening_on_a_day_off_is_reported_as_a_day_off():
    message = cycle_message([], days_off=[S("2026-10-14 AM")])
    assert message == Message("Elisa: opening Wed Oct 14 AM (day off)", "Not accepted, marked as a day off: Wed Oct 14 AM.")
