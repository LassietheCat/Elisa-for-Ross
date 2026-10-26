from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .detect import jobs_per_day, new_openings, openings_in, plan_month
from .models import ApplyOutcome, ApplyResult, MonthReading, Opening, Slot, SlotTags
from .notify import Notifier, build_notifier, cycle_message, missed_openings_message
from .site.applier import Applier
from .site.calendar import CalendarReader
from .site.errors import CalendarNotRendered, InvalidCredentials, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SiteSession
from .state import BotState, ControlStore, StateStore
from .timeutil import add_months, now_in, short_time, to_iso

log = logging.getLogger(__name__)

Clock = Callable[[], dt.datetime]


@dataclass
class CycleReport:
    available: set[Slot] = field(default_factory=set)
    new: set[Slot] = field(default_factory=set)
    baseline_only: bool = False
    results: list[ApplyResult] = field(default_factory=list)
    skipped_booked: list[Slot] = field(default_factory=list)
    skipped_day_off: list[Slot] = field(default_factory=list)
    skipped_too_close: list[Slot] = field(default_factory=list)
    skipped_day_full: list[Slot] = field(default_factory=list)
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
    control = ControlStore(settings).load()
    days_off = control.days_off_dates()
    report = CycleReport()
    baselines: list[bool] = []
    missed: list[str] = []
    try:
        if not session.started:
            await session.start()
        session.clear_problems()
        loaded_mark = session.list_mark()
        report.signed_in = await session.ensure_ready()
        reader = CalendarReader(session.page, selectors)
        applier = Applier(session.page, selectors, reader, test_mode=settings.test_mode)
        shown = await _shown_month(reader, session)
        if month is None:
            targets = [m for m in window if only and m in only] or list(window)
            months = reading_order(targets, shown)
        else:
            months = [month]
        for year, month_number in months:
            await reader.go_to_month(year, month_number)
            if not session.list_loaded_since(loaded_mark, (year, month_number)):
                reload_mark = session.list_mark()
                await reader.refresh()
                session.raise_for_problems()
                if not session.list_loaded_since(reload_mark, (year, month_number)):
                    session.warn_once(
                        ("list load",),
                        "Reloading the calendar did not reload its open work orders; reloading before every read",
                    )
            reading = await reader.read_displayed_month()
            session.raise_for_problems()
            missed += await _warn_about_missed_tags(session, reading)
            _note_job_changes(state, reading, clock())
            baseline = month_key((year, month_number)) in state.baseline_pending
            openings = openings_in(reading.slots, await session.open_order_times((year, month_number), loaded_mark))
            job_times = {} if baseline else await _job_times(session, reading, openings, state, today, days_off)
            plan = plan_month(
                reading.slots,
                state,
                today=today,
                settings=settings,
                openings=openings,
                known=session.open_times,
                clicked=session.clicked,
                job_times=job_times,
                max_jobs=control.max_jobs_per_day,
                already_planned=len(report.results),
                baseline=baseline,
                days_off=days_off,
            )
            session.remember_open_times(reading.slots, openings)
            report.months_read.append((year, month_number))
            report.open_count += sum(tags.available for tags in reading.slots.values())
            baselines.append(plan.baseline_only)
            report.available |= plan.available
            report.new |= plan.new
            report.skipped_booked += plan.skipped_booked
            report.skipped_day_off += plan.skipped_day_off
            report.skipped_too_close += plan.skipped_too_close
            report.skipped_day_full += plan.skipped_day_full
            for outcome, slots in (
                ("skipped", plan.skipped_booked),
                ("day off", plan.skipped_day_off),
                ("too close", plan.skipped_too_close),
                ("day full", plan.skipped_day_full),
            ):
                for slot in slots:
                    state.add_activity(to_iso(clock()), str(slot), outcome, _since(detected_at))
            for opening in plan.to_apply:
                slot = opening.slot
                result = await applier.apply_one(opening)
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
                    session.clicked.add((slot, opening.clock))
        if missed:
            await notifier.send(missed_openings_message(missed))
    except Exception as exc:
        error = exc if isinstance(exc, SiteError) else None
        if not isinstance(exc, (InvalidCredentials, VerificationRequired)):
            error = session.problem() or error
        state.last_run_at = to_iso(clock())
        state.last_run_ok = False
        state.last_error = error.code if error is not None else "unexpected_error"
        state.count(today, "failed")
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
    state.count(today, "reads")
    state.prune(today)
    store.save(state)

    report.notified = await _notify(
        notifier,
        settings,
        report.results,
        report.skipped_booked,
        report.skipped_day_off,
        report.skipped_too_close,
        report.skipped_day_full,
        control.max_jobs_per_day,
    )
    return report


async def _job_times(
    session: SiteSession,
    reading: MonthReading,
    openings: list[Opening],
    state: BotState,
    today: dt.date,
    days_off: set[dt.date],
) -> dict[dt.date, list[dt.datetime] | None]:
    jobs = jobs_per_day(reading.slots)
    days = sorted(
        {
            opening.slot.date
            for opening in new_openings(openings, state, session.open_times)
            if opening.slot.date >= today and opening.slot.date not in days_off
        }
    )
    return {day: (await session.job_times(day, today)) if jobs[day] else [] for day in days}


async def _shown_month(reader: CalendarReader, session: SiteSession) -> tuple[int, int]:
    try:
        return await reader.displayed_month()
    except CalendarNotRendered:
        shown = await reader.refresh()
        session.raise_for_problems()
        return shown


def _note_job_changes(state: BotState, reading: MonthReading, now: dt.datetime) -> None:
    key = month_key((reading.year, reading.month))
    jobs = {str(slot): tags.jobs() for slot, tags in sorted(reading.slots.items()) if tags.booked}
    before = state.jobs.get(key)
    state.jobs[key] = {"read_at": to_iso(now), "slots": jobs}
    if not isinstance(before, dict) or not isinstance(before.get("slots"), dict):
        log.info("%s: watching %d booked half-day(s) for new jobs", key, len(jobs))
        return
    earlier = before["slots"]
    try:
        when = short_time(dt.datetime.fromisoformat(before["read_at"]), now.date())
    except (KeyError, TypeError, ValueError):
        when = "the last read"
    for slot in sorted(earlier.keys() | jobs.keys()):
        was, current = earlier.get(slot, {}), jobs.get(slot, {})
        if was == current:
            continue
        change = sum(current.values()) - sum(was.values())
        log.info(
            "%s on the calendar: %s, now %s (was %s at %s)",
            "New job" if change > 0 else "Job gone" if change < 0 else "Job changed",
            Slot.parse(slot).label(),
            _describe_jobs(current),
            _describe_jobs(was),
            when,
        )


def _describe_jobs(jobs: dict[str, int]) -> str:
    return ", ".join(f"{count} {status.capitalize()}" for status, count in jobs.items()) or "none"


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


async def _notify(
    notifier: Notifier,
    settings: Settings,
    results: list[ApplyResult],
    skipped: list[Slot],
    days_off: list[Slot] = (),
    too_close: list[Slot] = (),
    day_full: list[Slot] = (),
    max_jobs: int = 3,
) -> bool:
    message = cycle_message(
        results,
        skipped=skipped if settings.notify_skipped else (),
        include_missed=settings.notify_missed,
        days_off=days_off if settings.notify_skipped else (),
        too_close=too_close if settings.notify_skipped else (),
        day_full=day_full if settings.notify_skipped else (),
        gap_hours=settings.job_gap_minutes / 60,
        max_jobs=max_jobs,
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
