from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .detect import plan_month
from .models import ApplyOutcome, ApplyResult, MonthReading, Slot, SlotTags
from .notify import Notifier, build_notifier, cycle_message, missed_openings_message
from .site.applier import Applier
from .site.calendar import CalendarReader
from .site.errors import InvalidCredentials, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SiteSession
from .state import BotState, StateStore
from .timeutil import add_months, now_in, to_iso

log = logging.getLogger(__name__)

Clock = Callable[[], dt.datetime]


@dataclass
class CycleReport:
    available: set[Slot] = field(default_factory=set)
    new: set[Slot] = field(default_factory=set)
    baseline_only: bool = False
    results: list[ApplyResult] = field(default_factory=list)
    skipped_booked: list[Slot] = field(default_factory=list)
    notified: bool = False
    months_read: list[tuple[int, int]] = field(default_factory=list)
    signed_in: bool = False
    open_count: int = 0


def month_key(month: tuple[int, int]) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def target_months(today: dt.date, count: int) -> list[tuple[int, int]]:
    return [add_months(today.year, today.month, offset) for offset in range(count)]


def reading_order(months: list[tuple[int, int]], displayed: tuple[int, int]) -> list[tuple[int, int]]:
    if displayed not in months:
        return list(months)
    index = months.index(displayed)
    if index == len(months) - 1:
        return list(reversed(months))
    return [displayed, *months[index + 1 :], *reversed(months[:index])]


def _since(detected_at: float | None) -> float | None:
    return None if detected_at is None else round(time.monotonic() - detected_at, 1)


def _clock(settings: Settings, now: Clock | None) -> Clock:
    return now or (lambda: now_in(settings.timezone))


async def run_check(settings: Settings, selectors: Selectors, *, now: Clock | None = None) -> dict[Slot, SlotTags]:
    clock = _clock(settings, now)
    session = SiteSession(settings, selectors)
    await session.start()
    try:
        await session.ensure_ready()
        reader = CalendarReader(session.page, selectors)
        await reader.refresh()
        slots = await reader.read_months(target_months(clock().date(), settings.months_to_check))
        session.raise_for_problems()
        return slots
    finally:
        await session.logout()
        await session.close()


async def run_once(
    settings: Settings,
    selectors: Selectors,
    *,
    notifier: Notifier | None = None,
    now: Clock | None = None,
    session: SiteSession | None = None,
    month: tuple[int, int] | None = None,
    only: list[tuple[int, int]] | None = None,
    detected_at: float | None = None,
) -> CycleReport:
    clock = _clock(settings, now)
    store = StateStore(settings.state_path)
    state = store.load()
    notifier = notifier or build_notifier(settings)
    own_session = session is None
    session = session or SiteSession(settings, selectors)
    today = clock().date()
    window = target_months(today, settings.months_to_check)
    state.baseline_pending += [
        key
        for key in map(month_key, window)
        if key not in state.baseline_pending and (state.watched_through is None or key > state.watched_through)
    ]
    report = CycleReport()
    baselines: list[bool] = []
    missed: list[str] = []
    try:
        if not session.started:
            await session.start()
        session.clear_problems()
        calendar_was_showing = await session.calendar_visible()
        report.signed_in = await session.ensure_ready()
        reader = CalendarReader(session.page, selectors)
        applier = Applier(session.page, selectors, reader, test_mode=settings.test_mode)
        if month is None and only:
            targets = [m for m in window if m in only] or list(window)
            displayed = await reader.displayed_month()
            if calendar_was_showing and displayed in targets:
                await reader.refresh()
                session.raise_for_problems()
            months = reading_order(targets, displayed)
        elif month is None:
            displayed = await reader.refresh()
            session.raise_for_problems()
            months = reading_order(window, displayed)
        else:
            if calendar_was_showing and await reader.displayed_month() == month:
                await reader.refresh()
            months = [month]
        for year, month_number in months:
            await reader.go_to_month(year, month_number)
            reading = await reader.read_displayed_month()
            session.raise_for_problems()
            missed += await _warn_about_missed_tags(session, reading)
            plan = plan_month(
                reading.slots,
                state,
                today=today,
                settings=settings,
                already_planned=len(report.results),
                baseline=month_key((year, month_number)) in state.baseline_pending,
            )
            report.months_read.append((year, month_number))
            report.open_count += sum(tags.available for tags in reading.slots.values())
            baselines.append(plan.baseline_only)
            report.available |= plan.available
            report.new |= plan.new
            report.skipped_booked += plan.skipped_booked
            for slot in plan.skipped_booked:
                state.add_activity(to_iso(clock()), str(slot), "skipped", _since(detected_at))
            for slot in plan.to_apply:
                result = await applier.apply_one(slot)
                seconds = _since(detected_at)
                log.info(
                    "Opening %s: %s%s%s",
                    slot,
                    result.outcome.value,
                    f" ({result.code})" if result.code else "",
                    f", {seconds} s after the quick check saw it" if seconds is not None else "",
                )
                state.add_activity(to_iso(clock()), str(slot), result.outcome.value, seconds)
                report.results.append(result)
                if result.clicked:
                    state.record_apply(slot, clock())
        if missed:
            await notifier.send(missed_openings_message(missed))
    except Exception as exc:
        error = exc if isinstance(exc, SiteError) else None
        if not isinstance(exc, (InvalidCredentials, VerificationRequired)):
            error = session.problem() or error
        state.last_run_at = to_iso(clock())
        state.last_run_ok = False
        state.last_error = error.code if error is not None else "unexpected_error"
        store.save(state)
        clicked = [result for result in report.results if result.clicked]
        if clicked:
            report.notified = await _notify(notifier, settings, clicked, ())
        if error is not None and error is not exc:
            raise error from exc
        raise
    finally:
        if own_session:
            await session.logout()
            await session.close()

    report.baseline_only = bool(baselines) and all(baselines)
    retry = _retry_next_time(report.results, state, settings)
    read_keys = {month_key(m) for m in report.months_read}
    other_months = {month_key(m) for m in window} - read_keys
    kept = {slot for slot in state.seen_slots() if month_key((slot.date.year, slot.date.month)) in other_months}
    state.seen = sorted(str(slot) for slot in kept | (report.available - retry))
    state.baseline_pending = [key for key in state.baseline_pending if key in other_months]
    state.baseline_done = not state.baseline_pending
    if read_keys:
        state.watched_through = max(read_keys | {state.watched_through or ""})
    if detected_at is not None and not report.new:
        seconds = _since(detected_at)
        state.add_activity(to_iso(clock()), None, "gone", seconds)
        log.info("The new opening was gone %s s after the quick check saw it (taken before the bot could read it)", seconds)
    log.info(
        "Checked %s: %d open, %d new%s",
        ", ".join(sorted(read_keys)),
        len(report.available),
        len(report.new),
        " (first look, recorded only)" if report.baseline_only else "",
    )
    state.last_run_at = to_iso(clock())
    state.last_run_ok = True
    state.last_error = None
    state.prune(today)
    store.save(state)

    report.notified = await _notify(notifier, settings, report.results, report.skipped_booked)
    return report


async def _warn_about_missed_tags(session: SiteSession, reading: MonthReading) -> list[str]:
    key = month_key((reading.year, reading.month))
    month_name = dt.date(reading.year, reading.month, 1).strftime("%B %Y")
    problems = []
    if reading.unknown_words:
        words = ", ".join(sorted(reading.unknown_words))
        if session.warn_once(("labels", key, words), "%s: calendar tags with labels the bot doesn't know: %s", key, words):
            problems.append(f"{month_name}: calendar tags with labels the bot doesn't know ({words}).")
    for day, count in sorted(reading.unread.items()):
        if session.warn_once(("unread", day, count), "%s: %d calendar tag(s) the bot couldn't read", day, count):
            problems.append(f"{day:%a %b} {day.day}: {count} calendar tag(s) the bot couldn't read.")
    site_open = await session.site_open_count()
    shown = sum(tags.available for tags in reading.slots.values())
    if site_open is not None and site_open != shown:
        newly = session.warn_once(
            ("open count", key, site_open, shown),
            "%s: Elisa's own count shows %d open work order(s), the calendar shows %d",
            key,
            site_open,
            shown,
        )
        if newly and site_open > shown:
            problems.append(f"{month_name}: Elisa lists {site_open} open work order(s), but the bot saw {shown} on the calendar.")
    return problems


async def _notify(notifier: Notifier, settings: Settings, results: list[ApplyResult], skipped: list[Slot]) -> bool:
    message = cycle_message(
        results,
        skipped=skipped if settings.notify_skipped else (),
        include_missed=settings.notify_missed,
    )
    if message is None:
        return False
    return await notifier.send(message)


def _retry_next_time(results: list[ApplyResult], state: BotState, settings: Settings) -> set[Slot]:
    retry = set()
    for result in results:
        if result.outcome is ApplyOutcome.ERROR and not result.clicked:
            key = str(result.slot)
            state.attempts[key] = state.attempts.get(key, 0) + 1
            if state.attempts[key] < settings.max_attempts_per_slot:
                retry.add(result.slot)
    return retry
