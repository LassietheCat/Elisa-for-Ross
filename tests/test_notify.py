from __future__ import annotations

import dataclasses
import http.client
import io
import json
import logging
import re
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


TEXTBELT_KEY = "textbelt-secret-value-7f3a"


@pytest.fixture
def sms_settings(tmp_path):
    def make(**overrides) -> Settings:
        fields = {
            "textbelt_key": TEXTBELT_KEY,
            "alert_sms_to": ("+15551112222", "+15553334444"),
            "data_dir": tmp_path,
        }
        return Settings(**(fields | overrides))

    return make


class EmailCapture:
    name = "email"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[Message] = []

    async def send(self, message: Message) -> bool:
        self.messages.append(message)
        return self.ok


@pytest.fixture
def fake_textbelt(monkeypatch):
    posts: list[dict] = []
    gets: list[str] = []
    lock = threading.Lock()

    def urlopen(request, timeout=None):
        with lock:
            fake_textbelt.calls += 1
            fake_textbelt.in_flight += 1
            fake_textbelt.most_at_once = max(fake_textbelt.most_at_once, fake_textbelt.in_flight)
        try:
            time.sleep(fake_textbelt.delay)
            if isinstance(fake_textbelt.fail, BaseException):
                raise fake_textbelt.fail
            if fake_textbelt.fail:
                raise OSError("textbelt unreachable")
            if fake_textbelt.status:
                raise urllib.error.HTTPError(request.full_url, fake_textbelt.status, "refused", None, None)
            if request.get_method() == "GET":
                gets.append(request.full_url)
                return io.BytesIO(json.dumps(fake_textbelt.quota).encode())
            form = {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode()).items()}
            posts.append({"url": request.full_url, "form": form})
            return io.BytesIO(json.dumps(fake_textbelt.reply).encode())
        finally:
            with lock:
                fake_textbelt.in_flight -= 1

    fake_textbelt.fail = False
    fake_textbelt.status = 0
    fake_textbelt.delay = 0.0
    fake_textbelt.calls = 0
    fake_textbelt.in_flight = 0
    fake_textbelt.most_at_once = 0
    fake_textbelt.reply = {"success": True, "quotaRemaining": 120, "textId": 1}
    fake_textbelt.quota = {"success": True, "quotaRemaining": 98}
    fake_textbelt.posts = posts
    fake_textbelt.gets = gets
    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    return fake_textbelt


async def test_text_is_sent_to_every_number(fake_textbelt, sms_settings, caplog):
    caplog.set_level(logging.DEBUG)
    sent = await notify.SmsNotifier(sms_settings()).send(
        Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM.")
    )
    assert sent
    assert [post["form"]["phone"] for post in fake_textbelt.posts] == ["+15551112222", "+15553334444"]
    post = fake_textbelt.posts[0]
    assert post["url"] == "https://textbelt.com/text"
    assert post["form"] == {
        "phone": "+15551112222",
        "message": "Elisa bot - applied Wed Oct 14 AM",
        "key": TEXTBELT_KEY,
        "sender": "Elisa bot",
    }
    assert "Alert text sent to 2 of 2 number(s), 120 texts left" in caplog.text
    assert TEXTBELT_KEY not in caplog.text


async def test_informational_alerts_do_not_send_a_text(fake_textbelt, sms_settings):
    message = cycle_message([result("2026-10-20 AM", ApplyOutcome.MISSED)])
    assert message.sms is False
    assert not await notify.SmsNotifier(sms_settings()).send(message)
    assert fake_textbelt.posts == []


def test_an_accepted_job_does_send_a_text():
    assert cycle_message([result("2026-10-14 AM", ApplyOutcome.APPLIED)]).sms is True
    assert cycle_message([result("2026-10-14 AM", ApplyOutcome.UNVERIFIED)]).sms is True
    assert paused_message("bad_password").sms is True
    assert trouble_message(3, "browser_error").sms is True


def test_a_text_is_the_subject_line_with_nothing_textbelt_could_take_for_a_link():
    texted = [
        cycle_message([result("2026-10-14 AM", ApplyOutcome.APPLIED), result("2026-10-15 PM", ApplyOutcome.UNVERIFIED)]),
        cycle_message([result("2026-10-15 PM", ApplyOutcome.UNVERIFIED)]),
        landing_message([check(["2026-11-13 PM"], 1), check(["2026-11-16 AM"], 1)]),
        landing_message([check(["2026-11-12 AM"], 0, unsure=True)]),
        *(paused_message(code) for code in ("verification_required", "cookie_expired", "bad_password")),
        *(trouble_message(3, code) for code in (*notify._PROBLEMS, "something_new")),
        recovered_message(3),
        notify.missed_openings_message(["The open-order list stopped answering."]),
        notify.test_message(),
    ]
    for message in texted:
        body = notify.sms_body(message)
        assert message.sms and body.isascii() and len(body) <= 160, body
        assert not re.search(r"[./:]", body), body
    assert notify.sms_body(texted[0]) == "Elisa bot - applied Wed Oct 14 AM"
    assert notify.sms_body(texted[2]) == "Elisa bot - not on your calendar, Fri Nov 13 PM (+1 more)"
    assert notify.sms_body(notify.test_message()) == "Elisa bot - test alert"


async def test_text_failure_retries_then_gives_up(fake_textbelt, sms_settings):
    fake_textbelt.fail = True
    notifier = notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",)), retries=2, retry_delay_s=0)
    assert not await notifier.send(Message("s", "t"))
    assert fake_textbelt.calls == 3 and fake_textbelt.posts == []


async def test_a_long_text_is_trimmed_to_what_textbelt_takes(fake_textbelt, sms_settings):
    await notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",))).send(Message("word " * 200, "t"))
    body = fake_textbelt.posts[0]["form"]["message"]
    assert notify.SMS_MAX_BYTES - 8 < len(body.encode()) <= notify.SMS_MAX_BYTES and body.endswith("...")
    accented = notify.sms_body(Message("\u00e9" * 200, "t"))
    assert len(accented.encode()) <= notify.SMS_MAX_BYTES and accented.endswith("\u00e9...")


def test_build_notifier_uses_whichever_channels_are_configured(sms_settings):
    assert isinstance(notify.build_notifier(Settings()), notify.ConsoleNotifier)
    assert isinstance(notify.build_notifier(email_settings()), EmailNotifier)
    alone = notify.build_notifier(sms_settings())
    assert isinstance(alone, notify.SmsNotifier) and alone.email is None
    both = notify.build_notifier(
        dataclasses.replace(email_settings(), textbelt_key=TEXTBELT_KEY, alert_sms_to=("+15551112222",))
    )
    assert isinstance(both, notify.MultiNotifier)
    assert [n.name for n in both.notifiers] == ["email", "sms"]
    assert both.notifiers[1].email is both.notifiers[0]


async def test_a_text_still_goes_out_when_email_is_down(fake_smtp, fake_textbelt, sms_settings):
    fake_smtp.fail = True
    both = notify.MultiNotifier(
        [EmailNotifier(email_settings(), retries=0), notify.SmsNotifier(sms_settings(), retries=0)]
    )
    assert await both.send(Message("Elisa: applied Wed Oct 14 AM", "Applied: Wed Oct 14 AM."))
    assert len(fake_textbelt.posts) == 2


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


async def test_an_email_only_message_counts_as_sent_only_when_the_email_went(fake_smtp, fake_textbelt, sms_settings):
    report = Message("Elisa: weekly report, Mon Nov 9 to Sun Nov 15", "Week of Mon Nov 9 to Sun Nov 15.", sms=False)
    both = notify.MultiNotifier([EmailNotifier(email_settings(), retries=0), notify.SmsNotifier(sms_settings(), retries=0)])
    fake_smtp.fail = True
    assert not await both.send(report)
    fake_smtp.fail = False
    assert await both.send(report)
    assert fake_textbelt.posts == []


async def test_texts_go_to_every_number_at_once(fake_textbelt, sms_settings):
    fake_textbelt.delay = 0.3
    numbers = ("+15551112222", "+15553334444", "+15555556666")
    assert await notify.SmsNotifier(sms_settings(alert_sms_to=numbers)).send(Message("s", "t"))
    assert fake_textbelt.most_at_once == 3 and len(fake_textbelt.posts) == 3


async def test_a_refused_text_is_not_retried_but_a_rate_limit_is(fake_textbelt, sms_settings, caplog):
    one = sms_settings(alert_sms_to=("+15551112222",))
    fake_textbelt.status = 401
    assert not await notify.SmsNotifier(one, retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert fake_textbelt.calls == 1
    fake_textbelt.status, fake_textbelt.calls = 429, 0
    assert not await notify.SmsNotifier(one, retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert fake_textbelt.calls == 3
    fake_textbelt.status, fake_textbelt.calls = 0, 0
    fake_textbelt.reply = {"success": False, "error": "Invalid phone number"}
    assert not await notify.SmsNotifier(one, retries=2, retry_delay_s=0).send(Message("s", "t"))
    assert fake_textbelt.calls == 1
    assert "Alert text refused by TextBelt: Invalid phone number" in caplog.text


async def test_a_refusal_that_names_the_key_is_logged_without_it(fake_textbelt, sms_settings, caplog):
    one = sms_settings(alert_sms_to=("+15551112222",))
    fake_textbelt.reply = {
        "success": False,
        "error": "Sorry, ability to send URLs via text is limited to verified accounts. "
        f"Please go to https://textbelt.com/whitelist?key={TEXTBELT_KEY}",
    }
    assert not await notify.SmsNotifier(one).send(Message("s", "t"))
    fake_textbelt.reply = {"success": False, "error": f"Key {TEXTBELT_KEY} is not valid"}
    assert not await notify.SmsNotifier(one).send(Message("s", "t"))
    assert "limited to verified accounts. Please go to https://textbelt.com/whitelist" in caplog.text
    assert "Key [REDACTED] is not valid" in caplog.text
    assert "secret-value" not in caplog.text and "key=" not in caplog.text


async def test_a_low_balance_is_emailed_once_without_the_whole_key(fake_textbelt, sms_settings):
    settings = sms_settings(alert_sms_to=("+15551112222",))
    email = EmailCapture()
    notifier = notify.SmsNotifier(settings, email=email)
    fake_textbelt.reply = {"success": True, "quotaRemaining": 51, "textId": 1}
    await notifier.send(Message("s", "t"))
    assert email.messages == []
    fake_textbelt.reply["quotaRemaining"] = 50
    await notifier.send(Message("s", "t"))
    [reminder] = email.messages
    assert reminder.subject == "Elisa: text balance low (50 texts left)"
    assert reminder.sms is False
    assert notify.TOP_UP_URL in reminder.text and f"ending in {TEXTBELT_KEY[-4:]}" in reminder.text
    assert TEXTBELT_KEY not in reminder.subject + reminder.text
    fake_textbelt.reply["quotaRemaining"] = 49
    await notifier.send(Message("s", "t"))
    await notify.SmsNotifier(settings, email=email).send(Message("s", "t"))
    assert len(email.messages) == 1
    assert notifier.balance.left() == 49


async def test_running_out_sends_one_more_email_and_a_top_up_re_arms_the_reminder(fake_textbelt, sms_settings):
    email = EmailCapture()
    notifier = notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",)), email=email)
    fake_textbelt.reply = {"success": True, "quotaRemaining": 3, "textId": 1}
    await notifier.send(Message("s", "t"))
    fake_textbelt.reply = {"success": False, "quotaRemaining": 0, "error": "Out of quota"}
    assert not await notifier.send(Message("s", "t"))
    assert not await notifier.send(Message("s", "t"))
    assert [m.subject for m in email.messages] == [
        "Elisa: text balance low (3 texts left)",
        "Elisa: text alerts stopped (no texts left)",
    ]
    assert "no texts left" in email.messages[1].text and email.messages[1].sms is False
    fake_textbelt.reply = {"success": True, "quotaRemaining": 30, "textId": 2}
    await notifier.send(Message("s", "t"))
    assert len(email.messages) == 2
    fake_textbelt.reply = {"success": False, "quotaRemaining": 0, "error": "Out of quota"}
    await notifier.send(Message("s", "t"))
    assert len(email.messages) == 3
    fake_textbelt.reply = {"success": True, "quotaRemaining": 500, "textId": 3}
    await notifier.send(Message("s", "t"))
    fake_textbelt.reply["quotaRemaining"] = 40
    await notifier.send(Message("s", "t"))
    assert len(email.messages) == 4 and email.messages[-1].subject == "Elisa: text balance low (40 texts left)"


async def test_a_reminder_that_could_not_be_emailed_is_tried_again(fake_textbelt, sms_settings):
    email = EmailCapture(ok=False)
    notifier = notify.SmsNotifier(sms_settings(alert_sms_to=("+15551112222",)), email=email)
    fake_textbelt.reply = {"success": True, "quotaRemaining": 20, "textId": 1}
    await notifier.send(Message("s", "t"))
    email.ok = True
    await notifier.send(Message("s", "t"))
    await notifier.send(Message("s", "t"))
    assert len(email.messages) == 2


async def test_the_balance_is_checked_at_startup_without_sending_a_text(fake_textbelt, sms_settings, caplog):
    caplog.set_level(logging.INFO)
    email = EmailCapture()
    both = notify.MultiNotifier([email, notify.SmsNotifier(sms_settings(), email=email)])
    assert await notify.check_text_balance(both) == 98
    assert fake_textbelt.gets == [f"https://textbelt.com/quota/{TEXTBELT_KEY}"]
    assert fake_textbelt.posts == [] and email.messages == []
    assert "Text balance: 98 texts left" in caplog.text
    fake_textbelt.quota = {"success": True, "quotaRemaining": 12}
    assert await notify.check_text_balance(both) == 12
    assert [m.subject for m in email.messages] == ["Elisa: text balance low (12 texts left)"]
    assert await notify.check_text_balance(EmailCapture()) is None
    assert TEXTBELT_KEY not in caplog.text


async def test_a_failed_balance_check_is_only_logged(fake_textbelt, sms_settings, caplog):
    notifier = notify.SmsNotifier(sms_settings())
    fake_textbelt.fail = True
    assert await notifier.check_balance() is None
    fake_textbelt.fail = http.client.IncompleteRead(b"")
    assert await notifier.check_balance() is None
    assert not await notifier.send(Message("s", "t"))
    fake_textbelt.fail = False
    fake_textbelt.quota = {"success": False}
    assert await notifier.check_balance() is None
    assert "Could not check the text balance (OSError)" in caplog.text
    assert "check TEXTBELT_KEY" in caplog.text
    assert notifier.balance.left() is None
    assert TEXTBELT_KEY not in caplog.text
