from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, Protocol

from .config import Settings
from .models import ApplyOutcome, ApplyResult, Slot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    subject: str
    text: str


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


def build_notifier(settings: Settings) -> Notifier:
    return EmailNotifier(settings) if settings.email_enabled else ConsoleNotifier()


def _labels(slots: Iterable[Slot]) -> str:
    return ", ".join(slot.label() for slot in sorted(slots))


_SUBJECTS = (
    (ApplyOutcome.APPLIED, "applied"),
    (ApplyOutcome.UNVERIFIED, "please check"),
    (ApplyOutcome.TEST_MODE, "test mode, opening"),
    (ApplyOutcome.MISSED, "missed"),
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
    gap_hours: float = 3,
    max_jobs: int = 3,
) -> Message | None:
    by_outcome = {outcome: [r.slot for r in results if r.outcome is outcome] for outcome in ApplyOutcome}
    if not include_missed:
        by_outcome[ApplyOutcome.MISSED] = []
    skipped = sorted(skipped)
    days_off = sorted(days_off)
    too_close = sorted(too_close)
    day_full = sorted(day_full)

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
    if by_outcome[ApplyOutcome.ERROR]:
        lines.append(f"Opening seen but Accept could not be tried, will retry: {_labels(by_outcome[ApplyOutcome.ERROR])}.")
    if skipped:
        lines.append(f"Skipped, you already have a job then: {_labels(skipped)}.")
    if too_close:
        lines.append(f"Not accepted, less than {gap_hours:g} hours from another job: {_labels(too_close)}.")
    if day_full:
        lines.append(f"Not accepted, already {max_jobs} jobs that day: {_labels(day_full)}.")
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
    if subject is None:
        subject = f"Elisa: opening {days_off[0].label()} (day off)"
    return Message(subject, " ".join(lines))


def paused_message(code: str) -> Message:
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
