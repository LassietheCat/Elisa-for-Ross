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
from .site.errors import SiteError
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


def target_months(today: dt.date) -> list[tuple[int, int]]:
    return [(today.year, today.month), add_months(today.year, today.month, 1)]


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
        return await reader.read_months(target_months(clock().date()))
    finally:
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
        await session.ensure_ready()
        reader = CalendarReader(session.page, selectors)
        applier = Applier(session.page, selectors, reader, test_mode=settings.test_mode)
        await reader.refresh()
        for year, month in target_months(today):
            await reader.go_to_month(year, month)
            reading = await reader.read_displayed_month()
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
        state.last_run_at = to_iso(clock())
        state.last_run_ok = False
        state.last_error = exc.code if isinstance(exc, SiteError) else "unexpected_error"
        store.save(state)
        raise
    finally:
        if own_session:
            await session.close()

    retry = _retry_next_time(report.results, state, settings)
    state.seen = sorted(str(slot) for slot in report.available - retry)
    state.baseline_done = True
    state.last_run_at = to_iso(clock())
    state.last_run_ok = True
    state.last_error = None
    state.prune(today)
    store.save(state)

    message = cycle_message(
        report.results,
        skipped=report.skipped_booked if settings.notify_skipped else (),
        include_missed=settings.notify_missed,
    )
    if message is not None:
        report.notified = await notifier.send(message)
    return report


def _retry_next_time(results: list[ApplyResult], state: BotState, settings: Settings) -> set[Slot]:
    retry = set()
    for result in results:
        if result.outcome is ApplyOutcome.ERROR and not result.clicked:
            key = str(result.slot)
            state.attempts[key] = state.attempts.get(key, 0) + 1
            if state.attempts[key] < settings.max_attempts_per_slot:
                retry.add(result.slot)
    return retry
