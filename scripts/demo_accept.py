from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import tempfile
from pathlib import Path

from elisa_bot.config import Settings
from elisa_bot.cycle import CycleReport, run_once
from elisa_bot.logging_setup import setup_logging
from elisa_bot.models import Half, Slot
from elisa_bot.notify import EmailNotifier, Message
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SiteSession
from elisa_bot.timeutil import now_in
from tests.mock_site.server import MOCK_EMAIL, MOCK_PASSWORD, MockServer

TIMEZONE = "America/New_York"


class DemoNotifier:
    name = "demo"

    def __init__(self, settings: Settings):
        self.email = EmailNotifier(settings) if settings.email_enabled else None

    async def send(self, message: Message) -> bool:
        print(f"\n    ALERT  {message.subject}\n           {message.text}\n", flush=True)
        if self.email is None:
            return True
        return await self.email.send(Message(f"DEMO: {message.subject}", message.text))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show the bot accepting an opening on the local imitation of Elisa. The real site is never contacted."
    )
    parser.add_argument("--headless", action="store_true", help="hide the browser (for a rehearsal)")
    parser.add_argument("--auto", action="store_true", help="don't wait for Enter between steps")
    parser.add_argument("--email-to", help="also email the alert, subject starting with DEMO:, via AgentMail")
    parser.add_argument("--smtp-password-file", default=str(Path.home() / ".config/elisa-bot/smtp_password"))
    return parser


def _weekday(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def _job(day: dt.date, half: Half, status: str) -> dict:
    return {"date": day.isoformat(), "half": half.value, "status": status}


def _say(text: str) -> None:
    print(f"\n=== {text}", flush=True)


async def _pause(auto: bool, text: str) -> None:
    if auto:
        await asyncio.sleep(2)
        return
    await asyncio.to_thread(input, f"\n--- {text} Press Enter to continue. ")


def _outcomes(report: CycleReport) -> None:
    for result in report.results:
        print(f"    {result.slot.label()}: {result.outcome.value}", flush=True)
    for slot in report.skipped_booked:
        print(f"    {slot.label()}: skipped, Anya already has a job then", flush=True)


async def demo(args: argparse.Namespace) -> None:
    today = now_in(TIMEZONE).date()
    opening = Slot(_weekday(today + dt.timedelta(days=4)), Half.PM)
    booked = Slot(_weekday(opening.date + dt.timedelta(days=1)), Half.AM)
    jobs = [
        _job(booked.date, Half.AM, "assigned"),
        _job(_weekday(today + dt.timedelta(days=1)), Half.AM, "confirmed"),
        _job(_weekday(today + dt.timedelta(days=8)), Half.PM, "assigned"),
    ]
    with MockServer() as mock, tempfile.TemporaryDirectory() as data_dir:
        mock.configure(today=today.isoformat(), loading_delay_ms=900)
        mock.set_work_orders(jobs)
        env = {
            "ELISA_BASE_URL": mock.base_url,
            "ELISA_EMAIL": MOCK_EMAIL,
            "ELISA_PASSWORD": MOCK_PASSWORD,
            "DATA_DIR": data_dir,
            "TIMEZONE": TIMEZONE,
            "HEADLESS": str(args.headless).lower(),
            "TEST_MODE": "false",
        }
        if args.email_to:
            env |= {
                "SMTP_PROVIDER": "agentmail",
                "SMTP_USERNAME": "anya-alerts@agentmail.to",
                "SMTP_PASSWORD_FILE": args.smtp_password_file,
                "ALERT_EMAIL_TO": args.email_to,
            }
        settings = Settings.from_env(env)
        setup_logging(settings, process="demo")
        notifier = DemoNotifier(settings)
        session = SiteSession(settings, Selectors())
        await session.start()
        try:
            _say("1. The bot signs in and takes its first look: it notes what is already open and accepts nothing.")
            report = await run_once(settings, Selectors(), notifier=notifier, session=session)
            print(f"    Months checked: {len(report.months_read)}, openings already listed: {len(report.available)}", flush=True)
            await _pause(args.auto, f"Next, two openings appear: {opening.label()} and {booked.label()}.")

            mock.set_work_orders([*jobs, _job(opening.date, opening.half, "available"), _job(booked.date, booked.half, "available")])
            _say(f"2. New openings: {opening.label()} (free) and {booked.label()} (Anya already has a job then).")
            report = await run_once(settings, Selectors(), notifier=notifier, session=session)
            _outcomes(report)
            await _pause(args.auto, "Next, the bot's following check.")

            _say(f"3. The next check: {opening.label()} now shows as Applied, so nothing new happens.")
            month = (opening.date.year, opening.date.month)
            report = await run_once(settings, Selectors(), notifier=notifier, session=session, month=month)
            if report.results or report.skipped_booked:
                _outcomes(report)
            else:
                print("    No new openings.", flush=True)
            print(f"    Accept clicks on the imitation site: {len(mock.stats()['accept_calls'])}", flush=True)
            await _pause(args.auto, "Demo finished.")
        finally:
            await session.close()


def main() -> None:
    asyncio.run(demo(_parser().parse_args()))


if __name__ == "__main__":
    main()
