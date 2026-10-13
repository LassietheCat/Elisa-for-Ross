from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .detect import plan_month
from .models import ApplyOutcome, ApplyResult, Slot, SlotTags
from .notify import Notifier, build_notifier, cycle_message
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


def target_months(today: dt.date, count: int) -> list[tuple[int, int]]:
    return [add_months(today.year, today.month, offset) for offset in range(count)]


def reading_order(months: list[tuple[int, int]], displayed: tuple[int, int]) -> list[tuple[int, int]]:
    if displayed not in months:
        return list(months)
    index = months.index(displayed)
    if index == len(months) - 1:
        return list(reversed(months))
    return [displayed, *months[index + 1 :], *reversed(months[:index])]


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
) -> CycleReport:
    clock = _clock(settings, now)
    store = StateStore(settings.state_path)
    state = store.load()
    notifier = notifier or build_notifier(settings)
    own_session = session is None
    session = session or SiteSession(settings, selectors)
    today = clock().date()
    report = CycleReport()
    try:
        if not session.started:
            await session.start()
        session.clear_problems()
        await session.ensure_ready()
        reader = CalendarReader(session.page, selectors)
        applier = Applier(session.page, selectors, reader, test_mode=settings.test_mode)
        displayed = await reader.refresh()
        session.raise_for_problems()
        for year, month in reading_order(target_months(today, settings.months_to_check), displayed):
            await reader.go_to_month(year, month)
            reading = await reader.read_displayed_month()
            session.raise_for_problems()
            plan = plan_month(reading.slots, state, today=today, settings=settings, already_planned=len(report.results))
            report.available |= plan.available
            report.new |= plan.new
            report.baseline_only = plan.baseline_only
            report.skipped_booked += plan.skipped_booked
            for slot in plan.to_apply:
                result = await applier.apply_one(slot)
                log.info("Opening %s: %s%s", slot, result.outcome.value, f" ({result.code})" if result.code else "")
                report.results.append(result)
                if result.clicked:
                    state.record_apply(slot, clock())
    except Exception as exc:
        error = exc if isinstance(exc, SiteError) else None
        if not isinstance(exc, (InvalidCredentials, VerificationRequired)):
            # A rate limit or failed data request explains a broken page better than the timeout it caused.
            error = session.problem() or error
        state.last_run_at = to_iso(clock())
        state.last_run_ok = False
        state.last_error = error.code if error is not None else "unexpected_error"
        store.save(state)
        # A clicked slot is never handled again, so report it now or it is never reported.
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

    retry = _retry_next_time(report.results, state, settings)
    state.seen = sorted(str(slot) for slot in report.available - retry)
    state.baseline_done = True
    state.last_run_at = to_iso(clock())
    state.last_run_ok = True
    state.last_error = None
    state.prune(today)
    store.save(state)

    report.notified = await _notify(notifier, settings, report.results, report.skipped_booked)
    return report


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
