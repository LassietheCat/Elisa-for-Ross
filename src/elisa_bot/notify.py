from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
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
        email["From"] = s.smtp_from or ""
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
) -> Message | None:
    by_outcome = {outcome: [r.slot for r in results if r.outcome is outcome] for outcome in ApplyOutcome}
    if not include_missed:
        by_outcome[ApplyOutcome.MISSED] = []
    skipped = sorted(skipped)

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
    if not lines:
        return None

    subject = None
    for outcome, wording in _SUBJECTS:
        slots = sorted(by_outcome[outcome])
        if slots:
            more = f" (+{len(slots) - 1} more)" if len(slots) > 1 else ""
            subject = f"Elisa: {wording} {slots[0].label()}{more}"
            break
    if subject is None:
        subject = f"Elisa: opening {skipped[0].label()} (already booked)"
    return Message(subject, " ".join(lines))


def test_message() -> Message:
    return Message(
        "Elisa: test alert",
        "Test alert from the Elisa bot. Real alerts contain only the date and AM/PM of an opening.",
    )
