from __future__ import annotations

import base64
import contextlib
import dataclasses
import logging
import smtplib
import socket
import threading
import time
import urllib.error
import urllib.parse

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from elisa_bot import notify
from elisa_bot.config import Settings
from elisa_bot.models import ApplyOutcome, ApplyResult, LandingCheck, Slot
from elisa_bot.notify import (
    EmailNotifier,
    Message,
    cycle_message,
    landing_message,
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


TWILIO_SID = "AC" + "0" * 30


def sms_settings(**overrides) -> Settings:
    fields = {
        "twilio_account_sid": TWILIO_SID,
        "twilio_auth_token": "twilio-secret-value",
        "twilio_from": "+15550000000",
        "alert_sms_to": ("+15551112222", "+15553334444"),
    }
    return Settings(**(fields | overrides))


@pytest.fixture
def fake_twilio(monkeypatch):
    posts: list[dict] = []
    lock = threading.Lock()

    def urlopen(request, timeout=None):
        with lock:
            fake_twilio.calls += 1
            fake_twilio.in_flight += 1
            fake_twilio.most_at_once = max(fake_twilio.most_at_once, fake_twilio.in_flight)
        try:
            time.sleep(fake_twilio.delay)
            if fake_twilio.fail:
                raise OSError("twilio unreachable")
            if fake_twilio.status:
                raise urllib.error.HTTPError(request.full_url, fake_twilio.status, "refused", None, None)
            posts.append(
                {
                    "url": request.full_url,
                    "method": request.get_method(),
                    "auth": request.get_header("Authorization"),
                    "form": {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode()).items()},
                }
            )
        finally:
            with lock:
                fake_twilio.in_flight -= 1
        return contextlib.nullcontext()

    fake_twilio.fail = False
    fake_twilio.status = 0
    fake_twilio.delay = 0.0
    fake_twilio.calls = 0
    fake_twilio.in_flight = 0
    fake_twilio.most_at_once = 0
    fake_twilio.posts = posts
    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    return fake_twilio


async def test_text_is_sent_to_every_number(fake_twilio, caplog):
    caplog.set_level(logging.DEBUG)
    sent = await notify.SmsNotifier(sms_settings()).send(
        Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM.")
    )
    assert sent
    assert [post["form"]["To"] for post in fake_twilio.posts] == ["+15551112222", "+15553334444"]
    post = fake_twilio.posts[0]
    assert post["method"] == "POST"
    assert post["url"] == f"https://api.twilio.com/2010-04-01/Accounts/{'AC' + '0' * 30}/Messages.json"
    assert post["form"]["From"] == "+15550000000"
    assert post["form"]["Body"] == "Elisa bot: Applied: Wed Oct 14 AM."
    user, _, token = base64.b64decode(post["auth"].removeprefix("Basic ")).decode().partition(":")
    assert user == "AC" + "0" * 30 and token == "twilio-secret-value"
    assert "twilio-secret-value" not in caplog.text


async def test_informational_alerts_do_not_send_a_text(fake_twilio):
    message = cycle_message([result("2026-10-20 AM", ApplyOutcome.MISSED)])
    assert message.sms is False
    assert not await notify.SmsNotifier(sms_settings()).send(message)
    assert fake_twilio.posts == []


async def test_an_accepted_job_does_send_a_text(fake_twilio):
    assert cycle_message([result("2026-10-14 AM", ApplyOutcome.APPLIED)]).sms is True
    assert cycle_message([result("2026-10-14 AM", ApplyOutcome.UNVERIFIED)]).sms is True
    assert paused_message("bad_password").sms is True
    assert trouble_message(3, "browser_error").sms is True


async def test_text_failure_retries_then_gives_up(fake_twilio):
    fake_twilio.fail = True
    notifier = notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",)), retries=2, retry_delay_s=0)
    assert not await notifier.send(Message("s", "t"))
    assert fake_twilio.posts == []


async def test_a_long_text_is_trimmed_to_two_segments(fake_twilio):
    await notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",))).send(Message("s", "word " * 200))
    body = fake_twilio.posts[0]["form"]["Body"]
    assert len(body) == notify.SMS_MAX_CHARS and body.endswith("...")
    assert body.isascii()


def test_build_notifier_uses_whichever_channels_are_configured():
    assert isinstance(notify.build_notifier(Settings()), notify.ConsoleNotifier)
    assert isinstance(notify.build_notifier(email_settings()), EmailNotifier)
    assert isinstance(notify.build_notifier(sms_settings()), notify.SmsNotifier)
    both = notify.build_notifier(
        dataclasses.replace(
            email_settings(),
            twilio_account_sid=TWILIO_SID,
            twilio_auth_token="twilio-secret-value",
            twilio_from="+15550000000",
            alert_sms_to=("+15551112222",),
        )
    )
    assert isinstance(both, notify.MultiNotifier)
    assert [n.name for n in both.notifiers] == ["email", "sms"]


async def test_a_text_still_goes_out_when_email_is_down(fake_smtp, fake_twilio):
    fake_smtp.fail = True
    both = notify.MultiNotifier(
        [EmailNotifier(email_settings(), retries=0), notify.SmsNotifier(sms_settings(), retries=0)]
    )
    assert await both.send(Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM."))
    assert len(fake_twilio.posts) == 2


def test_console_is_used_until_email_is_configured():
    assert isinstance(notify.build_notifier(Settings()), notify.ConsoleNotifier)
    assert isinstance(notify.build_notifier(email_settings()), notify.EmailNotifier)


def test_paused_message_explains_what_to_do():
    message = paused_message("invalid_credentials")
    assert message.subject == "Elisa: bot switched off (sign-in problem)"
    assert "invalid_credentials" in message.text and "switch the bot back on" in message.text


def test_robot_check_message_says_not_to_switch_back_on():
    message = paused_message("verification_required")
    assert message.subject == "Elisa: bot switched off (Elisa's robot check)"
    assert "password is fine" in message.text and "don't switch the bot back on" in message.text


def test_cookie_expired_message_says_to_export_fresh_cookies():
    message = paused_message("cookie_expired")
    assert message.subject == "Elisa: bot switched off (session expired)"
    assert "session expired" in message.text and "fresh cookies" in message.text


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
    assert message == Message(
        "Elisa: opening Wed Oct 14 AM (day off)",
        "Not accepted, marked as a day off: Wed Oct 14 AM.",
        sms=False,
    )


def test_openings_too_close_or_on_a_full_day_are_reported_by_date_and_half():
    message = cycle_message([], too_close=[S("2026-11-10 AM")], day_full=[S("2026-11-12 PM")], gap_hours=3, max_jobs=3)
    assert message.subject == "Elisa: opening Tue Nov 10 AM (too close to another job)"
    assert message.text == (
        "Not accepted, less than 3 hours from another job: Tue Nov 10 AM. "
        "Not accepted, already 3 jobs that day: Thu Nov 12 PM."
    )
    assert cycle_message([], day_full=[S("2026-11-12 PM")]).subject == "Elisa: opening Thu Nov 12 PM (day full)"


def test_a_refusal_and_an_opening_that_starts_too_soon_are_reported_plainly():
    refused = ApplyResult(S("2026-11-16 AM"), ApplyOutcome.REFUSED, "refused", clicked=True)
    message = cycle_message([refused], too_soon=[S("2026-10-27 AM")], notice_hours=2)
    assert message.subject == "Elisa: refused Mon Nov 16 AM"
    assert message.text == (
        "Not accepted, Elisa refused it: Mon Nov 16 AM. Not accepted, too soon (minimum notice 2 hours): Tue Oct 27 AM."
    )


def check(slots: list[str], missing: int, *, unsure: bool = False, minutes: int = 3) -> LandingCheck:
    parsed = tuple(S(slot) for slot in slots)
    return LandingCheck(parsed[0].date, parsed, missing, unsure, minutes)


def test_a_job_that_is_not_on_the_calendar_is_reported_by_email_and_text():
    message = landing_message([check(["2026-11-12 AM"], 1)])
    assert message == Message(
        "Elisa: not on your calendar, Thu Nov 12 AM",
        "Not on your calendar: Thu Nov 12 AM, 3 minutes after the bot clicked Accept. Please check Elisa.",
    )
    assert message.sms is True


def test_when_only_some_of_a_days_jobs_landed_the_alert_says_how_many_did_not():
    message = landing_message([check(["2026-11-27 PM", "2026-11-27 PM"], 1, minutes=1)])
    assert message.subject == "Elisa: not on your calendar, Fri Nov 27"
    assert message.text == (
        "Not on your calendar: 1 of 2 jobs accepted for Fri Nov 27, 1 minute after the bot clicked Accept. Please check Elisa."
    )


def test_a_please_check_that_landed_is_settled_and_a_plain_success_says_nothing():
    assert landing_message([check(["2026-11-12 AM"], 0)]) is None
    message = landing_message([check(["2026-11-12 AM"], 0, unsure=True)])
    assert message == Message("Elisa: on your calendar after all, Thu Nov 12 AM", "On your calendar after all: Thu Nov 12 AM.")
    assert message.sms is True


def test_a_missing_job_leads_the_subject_when_both_kinds_arrive_together():
    message = landing_message([check(["2026-11-12 AM"], 0, unsure=True), check(["2026-11-13 PM"], 1), check(["2026-11-16 AM"], 1)])
    assert message.subject == "Elisa: not on your calendar, Fri Nov 13 PM (+1 more)"
    assert message.text == (
        "Not on your calendar: Fri Nov 13 PM, 3 minutes after the bot clicked Accept. "
        "Not on your calendar: Mon Nov 16 AM, 3 minutes after the bot clicked Accept. "
        "Please check Elisa. On your calendar after all: Thu Nov 12 AM."
    )


async def test_an_email_only_message_counts_as_sent_only_when_the_email_went(fake_smtp, fake_twilio):
    report = Message("Elisa: weekly report, Mon Nov 9 to Sun Nov 15", "Week of Mon Nov 9 to Sun Nov 15.", sms=False)
    both = notify.MultiNotifier([EmailNotifier(email_settings(), retries=0), notify.SmsNotifier(sms_settings(), retries=0)])
    fake_smtp.fail = True
    assert not await both.send(report)
    fake_smtp.fail = False
    assert await both.send(report)
    assert fake_twilio.posts == []


async def test_texts_go_to_every_number_at_once(fake_twilio):
    fake_twilio.delay = 0.3
    numbers = ("+15551112222", "+15553334444", "+15555556666")
    assert await notify.SmsNotifier(sms_settings(alert_sms_to=numbers)).send(Message("s", "t"))
    assert fake_twilio.most_at_once == 3 and len(fake_twilio.posts) == 3


async def test_a_text_twilio_refuses_is_not_retried_but_a_rate_limit_is(fake_twilio):
    one = sms_settings(alert_sms_to=("+15551112222",))
    fake_twilio.status = 401
    assert not await notify.SmsNotifier(one, retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert fake_twilio.calls == 1
    fake_twilio.status, fake_twilio.calls = 429, 0
    assert not await notify.SmsNotifier(one, retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert fake_twilio.calls == 3
