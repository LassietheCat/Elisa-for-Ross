from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, Protocol

from .config import Settings
from .models import ApplyOutcome, ApplyResult, Slot

log = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"
SMS_MAX_CHARS = 320


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    sms: bool = True


class Notifier(Protocol):
    name: str

    async def send(self, message: Message) -> bool:
        ...


class ConsoleNotifier:
    name = "console"

    async def send(self, message: Message) -> bool:
        log.info("ALERT (email not configured) %s | %s", message.subject, message.text)
        return True


class EmailNotifier:
    name = "email"

    def __init__(self, settings: Settings, *, retries: int = 2, retry_delay_s: float = 2.0):
        self.settings = settings
        self.retries = retries
        self.retry_delay_s = retry_delay_s

    async def send(self, message: Message) -> bool:
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self._send, message)
                log.info("Alert email sent to %d recipient(s)", len(self.settings.alert_email_to))
                return True
            except (smtplib.SMTPException, OSError) as exc:
                log.warning("Alert email failed (%s), attempt %d of %d", type(exc).__name__, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(self.retry_delay_s)
        return False

    def _send(self, message: Message) -> None:
        s = self.settings
        email = EmailMessage()
        email["From"] = formataddr((s.smtp_from_name, s.smtp_from or ""))
        email["To"] = ", ".join(s.alert_email_to)
        email["Subject"] = message.subject
        email.set_content(message.text)
        context = ssl.create_default_context()
        if s.smtp_ssl:
            server = smtplib.SMTP_SSL(s.smtp_host or "", s.smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(s.smtp_host or "", s.smtp_port, timeout=20)
        try:
            if s.smtp_starttls and not s.smtp_ssl:
                server.starttls(context=context)
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password or "")
            server.send_message(email)
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                pass


def sms_body(message: Message) -> str:
    body = f"Elisa bot: {message.text}"
    if len(body) <= SMS_MAX_CHARS:
        return body
    return body[: SMS_MAX_CHARS - 3].rstrip() + "..."


class SmsNotifier:
    name = "sms"

    def __init__(self, settings: Settings, *, retries: int = 2, retry_delay_s: float = 2.0):
        self.settings = settings
        self.retries = retries
        self.retry_delay_s = retry_delay_s

    async def send(self, message: Message) -> bool:
        if not message.sms:
            return True
        body = sms_body(message)
        numbers = self.settings.alert_sms_to
        sent = 0
        for number in numbers:
            if await self._send_to(number, body):
                sent += 1
        if sent:
            log.info("Alert text sent to %d of %d number(s)", sent, len(numbers))
        return sent > 0

    async def _send_to(self, number: str, body: str) -> bool:
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self._post, number, body)
                return True
            except OSError as exc:
                log.warning("Alert text failed (%s), attempt %d of %d", type(exc).__name__, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(self.retry_delay_s)
        return False

    def _post(self, number: str, body: str) -> None:
        s = self.settings
        sid = s.twilio_account_sid or ""
        url = f"{TWILIO_API}/Accounts/{urllib.parse.quote(sid, safe='')}/Messages.json"
        data = urllib.parse.urlencode({"To": number, "From": s.twilio_from or "", "Body": body}).encode()
        auth = base64.b64encode(f"{sid}:{s.twilio_auth_token or ''}".encode()).decode()
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Authorization", f"Basic {auth}")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=20):
            pass


class MultiNotifier:
    name = "multi"

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    async def send(self, message: Message) -> bool:
        results = await asyncio.gather(*(n.send(message) for n in self.notifiers), return_exceptions=True)
        delivered = False
        for notifier, result in zip(self.notifiers, results):
            if isinstance(result, BaseException):
                log.warning("The %s alert raised %s", notifier.name, type(result).__name__)
            elif result:
                delivered = True
        return delivered


def build_notifier(settings: Settings) -> Notifier:
    notifiers: list[Notifier] = []
    if settings.email_enabled:
        notifiers.append(EmailNotifier(settings))
    if settings.sms_enabled:
        notifiers.append(SmsNotifier(settings))
    if not notifiers:
        return ConsoleNotifier()
    return notifiers[0] if len(notifiers) == 1 else MultiNotifier(notifiers)


def _labels(slots: Iterable[Slot]) -> str:
    return ", ".join(slot.label() for slot in sorted(slots))


_SUBJECTS = (
    (ApplyOutcome.APPLIED, "applied"),
    (ApplyOutcome.UNVERIFIED, "please check"),
    (ApplyOutcome.TEST_MODE, "test mode, opening"),
    (ApplyOutcome.MISSED, "missed"),
    (ApplyOutcome.REFUSED, "refused"),
    (ApplyOutcome.ERROR, "opening"),
)


def cycle_message(
    results: list[ApplyResult],
    *,
    skipped: Iterable[Slot] = (),
    include_missed: bool = True,
    days_off: Iterable[Slot] = (),
    too_close: Iterable[Slot] = (),
    day_full: Iterable[Slot] = (),
    too_soon: Iterable[Slot] = (),
    gap_hours: float = 3,
    max_jobs: int = 3,
    notice_hours: float = 0,
) -> Message | None:
    by_outcome = {outcome: [r.slot for r in results if r.outcome is outcome] for outcome in ApplyOutcome}
    if not include_missed:
        by_outcome[ApplyOutcome.MISSED] = []
    skipped = sorted(skipped)
    days_off = sorted(days_off)
    too_close = sorted(too_close)
    day_full = sorted(day_full)
    too_soon = sorted(too_soon)

    lines = []
    if by_outcome[ApplyOutcome.APPLIED]:
        lines.append(f"Applied: {_labels(by_outcome[ApplyOutcome.APPLIED])}.")
    if by_outcome[ApplyOutcome.UNVERIFIED]:
        lines.append(f"Clicked Accept, please check Elisa: {_labels(by_outcome[ApplyOutcome.UNVERIFIED])}.")
    if by_outcome[ApplyOutcome.TEST_MODE]:
        lines.append(
            "Test mode, found the Accept button but did not click it: "
            f"{_labels(by_outcome[ApplyOutcome.TEST_MODE])}."
        )
    if by_outcome[ApplyOutcome.MISSED]:
        lines.append(f"Missed, taken before Accept finished: {_labels(by_outcome[ApplyOutcome.MISSED])}.")
    if by_outcome[ApplyOutcome.REFUSED]:
        lines.append(f"Not accepted, Elisa refused it: {_labels(by_outcome[ApplyOutcome.REFUSED])}.")
    if by_outcome[ApplyOutcome.ERROR]:
        lines.append(f"Opening seen but Accept could not be tried, will retry: {_labels(by_outcome[ApplyOutcome.ERROR])}.")
    if skipped:
        lines.append(f"Skipped, you already have a job then: {_labels(skipped)}.")
    if too_close:
        lines.append(f"Not accepted, less than {gap_hours:g} hours from another job: {_labels(too_close)}.")
    if day_full:
        lines.append(f"Not accepted, already {max_jobs} jobs that day: {_labels(day_full)}.")
    if too_soon:
        lines.append(f"Not accepted, too soon (minimum notice {notice_hours:g} hours): {_labels(too_soon)}.")
    if days_off:
        lines.append(f"Not accepted, marked as a day off: {_labels(days_off)}.")
    if not lines:
        return None

    subject = None
    for outcome, wording in _SUBJECTS:
        slots = sorted(by_outcome[outcome])
        if slots:
            more = f" (+{len(slots) - 1} more)" if len(slots) > 1 else ""
            subject = f"Elisa: {wording} {slots[0].label()}{more}"
            break
    if subject is None and skipped:
        subject = f"Elisa: opening {skipped[0].label()} (already booked)"
    if subject is None and too_close:
        subject = f"Elisa: opening {too_close[0].label()} (too close to another job)"
    if subject is None and day_full:
        subject = f"Elisa: opening {day_full[0].label()} (day full)"
    if subject is None and too_soon:
        subject = f"Elisa: opening {too_soon[0].label()} (too soon)"
    if subject is None:
        subject = f"Elisa: opening {days_off[0].label()} (day off)"
    booked = by_outcome[ApplyOutcome.APPLIED] or by_outcome[ApplyOutcome.UNVERIFIED]
    return Message(subject, " ".join(lines), sms=bool(booked))


def paused_message(code: str) -> Message:
    if code == "verification_required":
        return Message(
            "Elisa: bot switched off (Elisa's robot check)",
            f"Elisa asked for a robot check or a code at sign-in ({code}), which the bot doesn't do. "
            "The bot switched itself off so it won't lock the account. The password is fine. "
            "Please don't switch the bot back on; tell Ross.",
        )
    if code == "cookie_expired":
        return Message(
            "Elisa: bot switched off (session expired)",
            "The saved Elisa session expired. The bot switched itself off. "
            "Anya needs to sign in to Elisa in her browser and export fresh cookies; tell Ross.",
        )
    return Message(
        "Elisa: bot switched off (sign-in problem)",
        f"Elisa did not accept the sign-in ({code}). The bot switched itself off so it won't lock the account. "
        "Fix the password, then switch the bot back on.",
    )


_PROBLEMS = {
    "site_busy": "Elisa asked the bot to slow down (too many requests).",
    "sign_in_paused": (
        "The bot had to sign in again too often, so it paused signing in for a while. "
        "This may mean someone else is signing in to the same account."
    ),
    "data_request_failed": "Elisa's server returned an error while loading the calendar.",
    "calendar_not_rendered": "The Open Work Orders calendar did not load.",
    "navigation_failed": "The Open Work Orders page did not load.",
    "login_failed": "The Elisa sign-in page did not work.",
    "browser_error": "The bot's web browser had a problem.",
}


def trouble_message(failures: int, code: str) -> Message:
    reason = _PROBLEMS.get(code, f"Unexpected problem ({code}).")
    return Message(
        "Elisa: bot can't check right now",
        f"The last {failures} checks failed. {reason} The bot keeps trying and waits longer between tries. "
        "You'll get another email when it's working again.",
    )


def recovered_message(failures: int) -> Message:
    return Message("Elisa: bot is working again", f"Checks are working again after {failures} failed tries.")


def missed_openings_message(problems: list[str]) -> Message:
    return Message(
        "Elisa: the bot may be missing openings",
        " ".join(problems) + " Please check Open Work Orders in Elisa by hand. The bot keeps checking.",
    )


def test_message() -> Message:
    return Message(
        "Elisa: test alert",
        "Test alert from the Elisa bot. Real alerts contain only the date and AM/PM of an opening.",
    )
