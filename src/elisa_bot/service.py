from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import time
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .cycle import run_once, target_months
from .notify import Notifier, build_notifier, paused_message, recovered_message, trouble_message
from .site.errors import DataRequestFailed, InvalidCredentials, SignInPaused, SiteBusy, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SignInLimiter, SiteSession
from .state import ControlStore
from .timeutil import now_in

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 300
MAX_BUSY_BACKOFF_S = 900
MAX_RETRY_AFTER_S = 3600
ALERT_AFTER_FAILURES = 3
QUICK_SILENCE_S = 300
QUICK_MAX_S = 120
QUICK_RECOVER_S = 1800


def next_delay(failures: int, poll_seconds: int, *, busy: bool = False, retry_after: int | None = None) -> int:
    if failures <= 0:
        return poll_seconds
    if not busy:
        return min(MAX_BACKOFF_S, poll_seconds * 2 ** min(failures, 5))
    delay = min(MAX_BUSY_BACKOFF_S, poll_seconds * 2 ** min(failures + 1, 8))
    return max(delay, min(retry_after or 0, MAX_RETRY_AFTER_S))


class MonthRotation:
    def __init__(self) -> None:
        self.step = 0

    def reset(self) -> None:
        self.step = 0

    def next(self, months: list[tuple[int, int]]) -> tuple[int, int]:
        count = len(months)
        pattern = list(range(count)) + list(range(count - 2, 0, -1))
        target = months[pattern[self.step % len(pattern)]]
        self.step += 1
        return target


class QuickCheck:
    def __init__(
        self, settings: Settings, clock: Callable[[], dt.datetime], timer: Callable[[], float] = time.monotonic
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.timer = timer
        self.known: int | None = None
        self.paused = False
        self.misses = 0
        self.every = settings.quick_check_seconds
        self.refused_at: float | None = None

    def reset(self) -> None:
        self.known = None
        self.paused = False
        self.misses = 0

    def calendar_read(self) -> None:
        self.paused = False

    async def check(self, session: SiteSession) -> bool:
        if self.paused:
            return False
        base = self.settings.quick_check_seconds
        if self.every > base and self.refused_at is not None and self.timer() - self.refused_at >= QUICK_RECOVER_S:
            self.every = base
            log.info("Quick checks back to every %d s", base)
        today = self.clock().date()
        months = target_months(today, self.settings.months_to_check)
        last_year, last_month = months[-1]
        start = dt.date(months[0][0], months[0][1], 1)
        end = dt.date(last_year, last_month, calendar.monthrange(last_year, last_month)[1])
        try:
            count = await session.count_open(start, end, today)
        except SiteBusy:
            self.paused = True
            self.refused_at = self.timer()
            self.every = min(QUICK_MAX_S, self.every * 2)
            log.warning(
                "Quick check refused by the site (429); pausing until the next calendar read, then every %d s", self.every
            )
            return False
        if count is None:
            self.misses += 1
            if self.misses == max(1, QUICK_SILENCE_S // max(1, self.every)):
                log.warning("Quick checks got no answer from Elisa for %d s; relying on calendar reads", QUICK_SILENCE_S)
            return False
        self.misses = 0
        previous, self.known = self.known, count
        if previous is None:
            log.info("Quick checks running: Elisa lists %d open work order(s) in the months checked", count)
        elif count > previous:
            log.info("Elisa now lists %d open work order(s) in the months checked (was %d); reading them now", count, previous)
            return True
        return False


class FailureAlerts:
    def __init__(self, notifier: Notifier, *, after: int = ALERT_AFTER_FAILURES):
        self.notifier = notifier
        self.after = after
        self.failures = 0
        self.alerted = False

    async def failed(self, code: str) -> None:
        self.failures += 1
        if self.failures >= self.after and not self.alerted:
            log.warning("%d checks failed in a row; sending one alert", self.failures)
            self.alerted = await self.notifier.send(trouble_message(self.failures, code))

    async def succeeded(self) -> None:
        if self.alerted:
            log.info("Checks are working again after %d failures", self.failures)
            await self.notifier.send(recovered_message(self.failures))
        self.reset()

    def reset(self) -> None:
        self.failures = 0
        self.alerted = False


async def run_forever(
    settings: Settings,
    selectors: Selectors,
    *,
    stop: asyncio.Event,
    notifier: Notifier | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> None:
    clock = now or (lambda: now_in(settings.timezone))
    notifier = notifier or build_notifier(settings)
    controls = ControlStore(settings)
    sign_ins = SignInLimiter(settings.max_sign_ins_per_hour)
    alerts = FailureAlerts(notifier)
    rotation = MonthRotation()
    quick = QuickCheck(settings, clock)
    read_all = False
    loop = asyncio.get_running_loop()
    session: SiteSession | None = None
    switched_on: bool | None = None
    log.info("Bot running in %s; waiting for the switch", "test mode" if settings.test_mode else "LIVE mode")
    try:
        while not stop.is_set():
            control = controls.load()
            if not control.enabled:
                if switched_on is not False:
                    log.info("Switched off")
                    switched_on = False
                if session is not None:
                    session = await _shut(session, logout=settings.logout_when_off)
                alerts.reset()
                await _sleep(stop, 1.0)
                continue
            if switched_on is not True:
                if settings.quick_check_seconds:
                    log.info("Switched on; quick check every %d s, calendar every %d s", settings.quick_check_seconds, control.poll_seconds)
                else:
                    log.info("Switched on; checking every %d s", control.poll_seconds)
                switched_on = True
            started = loop.time()
            busy = False
            retry_after = None
            at_least = 0
            try:
                if session is None:
                    session = SiteSession(settings, selectors, sign_ins=sign_ins)
                    await session.start()
                    rotation.reset()
                    quick.reset()
                month = None if read_all else rotation.next(target_months(clock().date(), settings.months_to_check))
                read_all = False
                report = await run_once(settings, selectors, notifier=notifier, now=clock, session=session, month=month)
                quick.calendar_read()
                if report.signed_in:
                    at_least = settings.quiet_after_sign_in_seconds
                    started = loop.time()
                await alerts.succeeded()
            except (InvalidCredentials, VerificationRequired) as exc:
                log.warning("Elisa did not accept the sign-in (%s); switching the bot off", exc.code)
                controls.update(by="bot", now=clock(), enabled=False)
                session = await _shut(session, logout=False)
                await notifier.send(paused_message(exc.code))
                continue
            except (SiteBusy, SignInPaused) as exc:
                busy, retry_after = True, exc.retry_after
                started = loop.time()
                await alerts.failed(exc.code)
                log.warning("Check failed (%s), %d in a row; waiting longer: %s", exc.code, alerts.failures, exc)
            except SiteError as exc:
                await alerts.failed(exc.code)
                log.warning("Check failed (%s), %d in a row: %s", exc.code, alerts.failures, exc)
                if not isinstance(exc, DataRequestFailed):
                    session = await _recover(session, settings, selectors)
                    rotation.reset()
                    at_least = settings.quiet_after_sign_in_seconds
                    started = loop.time()
            except PlaywrightError as exc:
                await alerts.failed("browser_error")
                log.warning(
                    "Browser problem (%s), %d in a row; restarting the browser", type(exc).__name__, alerts.failures
                )
                session = await _shut(session, logout=False)
            except Exception:
                await alerts.failed("unexpected_error")
                log.exception("Unexpected error, %d in a row; restarting the browser", alerts.failures)
                session = await _shut(session, logout=False)
            can_quick = session is not None and settings.quick_check_seconds > 0 and not busy and not at_least
            read_all = await _wait_for_next(
                stop,
                controls,
                started,
                alerts.failures,
                busy=busy,
                retry_after=retry_after,
                at_least=at_least,
                quick=(lambda: quick.check(session)) if can_quick else None,
                quick_every=quick.every,
            )
    finally:
        if session is not None:
            await _shut(session, logout=settings.logout_when_off)
        log.info("Bot stopped")


async def _shut(session: SiteSession, *, logout: bool) -> None:
    try:
        if logout:
            await session.logout()
    finally:
        await session.close()
    return None


async def _recover(session: SiteSession, settings: Settings, selectors: Selectors) -> SiteSession | None:
    try:
        await session.page.goto(settings.base_url + selectors.dashboard_path, wait_until="domcontentloaded")
        return session
    except PlaywrightError:
        return await _shut(session, logout=False)


async def _wait_for_next(
    stop: asyncio.Event,
    controls: ControlStore,
    started: float,
    failures: int,
    *,
    busy: bool = False,
    retry_after: int | None = None,
    at_least: int = 0,
    quick: Callable[[], Awaitable[bool]] | None = None,
    quick_every: float = 0,
) -> bool:
    loop = asyncio.get_running_loop()
    next_quick = loop.time() + quick_every
    while not stop.is_set():
        control = controls.load()
        if not control.enabled:
            return False
        delay = max(at_least, next_delay(failures, control.poll_seconds, busy=busy, retry_after=retry_after))
        remaining = started + delay - loop.time()
        if remaining <= 0:
            return False
        if quick is not None and loop.time() >= next_quick and remaining > 2:
            next_quick = loop.time() + quick_every
            if await quick():
                return True
            continue
        until_quick = next_quick - loop.time() if quick is not None else remaining
        await _sleep(stop, max(0.05, min(1.0, remaining, until_quick)))
    return False


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
