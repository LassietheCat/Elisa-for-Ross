from __future__ import annotations

import asyncio
import http.client
import json
import logging
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, Protocol

from .config import Settings
from .logging_setup import REDACTED
from .models import ApplyOutcome, ApplyResult, LandingCheck, Slot
from .state import write_json
from .timeutil import now_in, to_iso

log = logging.getLogger(__name__)

TEXTBELT_URL = "https://textbelt.com"
TOP_UP_URL = "https://textbelt.com/purchase/"
SMS_SENDER = "Elisa bot"
SMS_MAX_BYTES = 256


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
    body = f"{SMS_SENDER} - {message.subject.removeprefix('Elisa: ')}"
    if len(body.encode()) <= SMS_MAX_BYTES:
        return body
    return body.encode()[: SMS_MAX_BYTES - 3].decode("utf-8", "ignore").rstrip() + "..."


class TextBalance:
    def __init__(self, settings: Settings):
        self.path = settings.sms_path
        self.timezone = settings.timezone

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def left(self) -> int | None:
        left = self.read().get("left")
        return left if isinstance(left, int) else None

    def write(self, left: int, warned: str | None) -> None:
        try:
            write_json(self.path, {"left": left, "checked_at": to_iso(now_in(self.timezone)), "warned": warned})
        except OSError as exc:
            log.warning("Could not record the text balance (%s)", type(exc).__name__)


def _reminder(left: int, low: int, warned: str | None) -> tuple[str | None, str | None]:
    if left > low:
        return None, None
    if left <= 0:
        return (None, "empty") if warned == "empty" else ("empty", warned)
    return ("low", None) if warned is None else (None, "low")


def _refusal(reply: dict, key: str | None) -> str:
    reason = re.sub(r"(https?://[^\s?]*)\?\S*", r"\1", str(reply.get("error") or "no reason given"))
    return (reason.replace(key, REDACTED) if key else reason)[:200]


class SmsNotifier:
    name = "sms"

    def __init__(
        self, settings: Settings, *, email: Notifier | None = None, retries: int = 2, retry_delay_s: float = 2.0
    ):
        self.settings = settings
        self.email = email
        self.balance = TextBalance(settings)
        self.retries = retries
        self.retry_delay_s = retry_delay_s

    async def send(self, message: Message) -> bool:
        if not message.sms:
            return False
        body = sms_body(message)
        numbers = self.settings.alert_sms_to
        results = await asyncio.gather(*(self._send_to(number, body) for number in numbers))
        sent = sum(ok for ok, _ in results)
        left = min((count for _, count in results if count is not None), default=None)
        if sent:
            more = "" if left is None else f", {left} texts left"
            log.info("Alert text sent to %d of %d number(s)%s", sent, len(numbers), more)
        if left is not None:
            await self._note_balance(left)
        return sent > 0

    async def check_balance(self) -> int | None:
        key = urllib.parse.quote(self.settings.textbelt_key or "", safe="")
        try:
            reply = await asyncio.to_thread(self._reply, urllib.request.Request(f"{TEXTBELT_URL}/quota/{key}"))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            log.warning("Could not check the text balance (%s)", type(exc).__name__)
            return None
        left = reply.get("quotaRemaining")
        if reply.get("success") is not True or not isinstance(left, int):
            log.warning("TextBelt did not give a balance for the key; check TEXTBELT_KEY")
            return None
        log.info("Text balance: %d texts left", left)
        await self._note_balance(left)
        return left

    async def _note_balance(self, left: int) -> None:
        due, warned = _reminder(left, self.settings.text_low_balance, self.balance.read().get("warned"))
        if due is not None:
            log.warning("%d texts left on the TextBelt key; emailing a top-up reminder", left)
            if self.email is not None and await self.email.send(balance_message(left, self.settings.textbelt_key)):
                warned = due
        self.balance.write(left, warned)

    async def _send_to(self, number: str, body: str) -> tuple[bool, int | None]:
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                reply = await asyncio.to_thread(self._post, number, body)
            except urllib.error.HTTPError as exc:
                log.warning("Alert text refused by TextBelt (HTTP %d), attempt %d of %d", exc.code, attempt, attempts)
                if 400 <= exc.code < 500 and exc.code != 429:
                    return False, None
            except (OSError, ValueError, http.client.HTTPException) as exc:
                log.warning("Alert text failed (%s), attempt %d of %d", type(exc).__name__, attempt, attempts)
            else:
                left = reply.get("quotaRemaining")
                left = left if isinstance(left, int) else None
                if reply.get("success") is True:
                    return True, left
                log.warning("Alert text refused by TextBelt: %s", _refusal(reply, self.settings.textbelt_key))
                return False, left
            if attempt < attempts:
                await asyncio.sleep(self.retry_delay_s)
        return False, None

    def _post(self, number: str, body: str) -> dict:
        form = {"phone": number, "message": body, "key": self.settings.textbelt_key or "", "sender": SMS_SENDER}
        request = urllib.request.Request(f"{TEXTBELT_URL}/text", data=urllib.parse.urlencode(form).encode(), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._reply(request)

    @staticmethod
    def _reply(request: urllib.request.Request) -> dict:
        with urllib.request.urlopen(request, timeout=20) as response:
            reply = json.loads(response.read().decode("utf-8"))
        if not isinstance(reply, dict):
            raise ValueError("TextBelt sent an unexpected reply")
        return reply


class MultiNotifier:
    name = "multi"

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    async def send(self, message: Message) -> bool:
        results = await asyncio.gather(*(n.send(message) for n in self.notifiers), return_exceptions=True)
        delivered = False
        for notifier, result in zip(self.notifiers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("The %s alert raised %s", notifier.name, type(result).__name__)
            elif result:
                delivered = True
        return delivered


def build_notifier(settings: Settings) -> Notifier:
    notifiers: list[Notifier] = []
    email = EmailNotifier(settings) if settings.email_enabled else None
    if email is not None:
        notifiers.append(email)
    if settings.sms_enabled:
        notifiers.append(SmsNotifier(settings, email=email))
    if not notifiers:
        return ConsoleNotifier()
    return notifiers[0] if len(notifiers) == 1 else MultiNotifier(notifiers)


async def check_text_balance(notifier: Notifier) -> int | None:
    for part in notifier.notifiers if isinstance(notifier, MultiNotifier) else [notifier]:
        if isinstance(part, SmsNotifier):
            return await part.check_balance()
    return None


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


def landing_message(checks: Iterable[LandingCheck]) -> Message | None:
    checks = list(checks)
    missing = [check for check in checks if check.missing]
    settled = [check for check in checks if not check.missing and check.unsure]
    lines = []
    for check in missing:
        what = check.label() if len(check.slots) == 1 else f"{check.missing} of {len(check.slots)} jobs accepted for {check.label()}"
        lines.append(f"Not on your calendar: {what}, {_minutes(check.minutes)} after the bot clicked Accept.")
    if missing:
        lines.append("Please check Elisa.")
    if settled:
        lines.append(f"On your calendar after all: {', '.join(check.label() for check in settled)}.")
    if not lines:
        return None
    shown = missing or settled
    more = f" (+{len(shown) - 1} more)" if len(shown) > 1 else ""
    wording = "not on your calendar" if missing else "on your calendar after all"
    return Message(f"Elisa: {wording}, {shown[0].label()}{more}", " ".join(lines))


def _minutes(count: int) -> str:
    return f"{count} minute{'' if count == 1 else 's'}"


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


def balance_message(left: int, key: str | None) -> Message:
    hint = f" (the one ending in {key[-4:]})" if key and len(key) >= 16 else ""
    top_up = f"To add texts, open {TOP_UP_URL} and top up the bot's TextBelt key{hint}."
    if left <= 0:
        return Message(
            "Elisa: text alerts stopped (no texts left)",
            f"The bot's TextBelt key has no texts left, so alert texts have stopped. Emails still go out. {top_up} "
            "Texts start again by themselves after the top-up.",
            sms=False,
        )
    return Message(
        f"Elisa: text balance low ({left} texts left)",
        f"The bot's TextBelt key has {left} texts left. When it reaches 0, texts stop; emails still go out. {top_up} "
        "You'll get this reminder once; another email comes if the texts run out.",
        sms=False,
    )
