from __future__ import annotations

import argparse
import asyncio
import calendar
import dataclasses
import sys
from pathlib import Path

from .config import ConfigError, Settings
from .cycle import CycleReport, run_check, run_once
from .livecheck import SiteCheckReport, run_site_check
from .logging_setup import setup_logging
from .models import Slot, SlotTags
from .notify import build_notifier, test_message
from .site.errors import InvalidCredentials, SiteError, VerificationRequired
from .site.selectors import load_selectors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elisa-bot", description="Elisa Open Work Orders bot.")
    parser.add_argument("--env-file", default=".env", help="settings file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="sign in and show what this month and next month look like (read-only)")
    sub.add_parser(
        "site-check",
        help="read-only check that the reader fits the live page: structure, both months, cross-check with the site's data",
    )
    once = sub.add_parser("once", help="one full run: read, find new openings, accept, alert")
    mode = once.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="really click Accept (overrides TEST_MODE)")
    mode.add_argument("--test-mode", action="store_true", help="find the Accept button but don't click it")
    sub.add_parser("test-email", help="send a test alert")
    reset = sub.add_parser("reset-state", help="forget saved openings; the next run records a fresh baseline")
    reset.add_argument("--yes", action="store_true", help="confirm")
    return parser


def _print_slots(slots: dict[Slot, SlotTags]) -> None:
    by_month: dict[tuple[int, int], list[tuple[Slot, SlotTags]]] = {}
    for slot, tags in slots.items():
        by_month.setdefault((slot.date.year, slot.date.month), []).append((slot, tags))
    for (year, month), items in sorted(by_month.items()):
        totals = {status: sum(getattr(tags, status) for _, tags in items) for status in ("available", "applied", "assigned", "confirmed")}
        print(f"{calendar.month_name[month]} {year}: " + ", ".join(f"{count} {status}" for status, count in totals.items()))
        openings = sorted(slot for slot, tags in items if tags.available)
        print("  Openings: " + (", ".join(slot.label() for slot in openings) if openings else "none"))


def _print_report(report: CycleReport, settings: Settings) -> None:
    mode = "TEST MODE" if settings.test_mode else "LIVE"
    if report.baseline_only:
        print(f"[{mode}] First run: recorded {len(report.available)} opening(s) already showing; nothing accepted.")
        return
    if not report.results and not report.skipped_booked:
        print(f"[{mode}] No new openings.")
    for result in report.results:
        print(f"[{mode}] {result.slot.label()}: {result.outcome.value}" + (f" ({result.code})" if result.code else ""))
    for slot in report.skipped_booked:
        print(f"[{mode}] {slot.label()}: skipped (already booked then)")


def _print_site_check(report: SiteCheckReport) -> None:
    print("Elisa site check (read-only)")
    width = max((len(step.name) for step in report.steps), default=0)
    for step in report.steps:
        mark = "OK  " if step.ok else "FAIL"
        print(f"  {mark} {step.name.ljust(width)}  {step.detail}".rstrip())
    openings = sorted(set(report.openings))
    print("Openings showing: " + (", ".join(slot.label() for slot in openings) if openings else "none"))
    if report.mismatches:
        print("Differences between the calendar and the site's data:")
        for line in report.mismatches[:30]:
            print(f"  - {line}")
        if len(report.mismatches) > 30:
            print(f"  ... and {len(report.mismatches) - 30} more")
    for warning in report.warnings:
        print(f"Note: {warning}")
    if report.diagnostics:
        print("Diagnostics at the failing step:")
        for key, value in report.diagnostics.items():
            print(f"  {key}: {value}")
    if report.http_errors:
        print("The site answered these requests with an error:")
        for line in report.http_errors[:20]:
            print(f"  {line}")
    print("Result: the reader fits the live page." if report.ok else "Result: something needs fixing (see above).")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(env_file=Path(args.env_file))
        selectors = load_selectors(settings.selectors_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "once" and (args.live or args.test_mode):
        settings = dataclasses.replace(settings, test_mode=not args.live)
    setup_logging(settings, process=args.command)

    if args.command == "reset-state":
        if not args.yes:
            print("This forgets all saved openings; the next run records a fresh baseline. Re-run with --yes.", file=sys.stderr)
            return 1
        settings.state_path.unlink(missing_ok=True)
        print("State reset.")
        return 0
    if args.command == "test-email":
        sent = asyncio.run(build_notifier(settings).send(test_message()))
        print("Test alert sent." if sent else "Test alert failed; see the log.")
        return 0 if sent else 1

    try:
        if args.command == "check":
            _print_slots(asyncio.run(run_check(settings, selectors)))
        elif args.command == "site-check":
            report = asyncio.run(run_site_check(settings, selectors))
            _print_site_check(report)
            return 0 if report.ok else 1
        else:
            _print_report(asyncio.run(run_once(settings, selectors)), settings)
        return 0
    except (InvalidCredentials, VerificationRequired) as exc:
        print(f"Elisa did not accept the sign-in ({exc.code}). Not retrying; check the credentials.", file=sys.stderr)
        return 3
    except (SiteError, ConfigError) as exc:
        print(f"Run failed: {getattr(exc, 'code', exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
