from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import time
import urllib.request
from collections import Counter
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .cycle import run_once, target_months
from .notify import Notifier, build_notifier, paused_message, recovered_message, trouble_message
from .site.errors import DataRequestFailed, InvalidCredentials, SignInPaused, SiteBusy, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SignInLimiter, SiteSession
from .state import ControlStore, write_json
from .timeutil import now_in, to_iso

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 300
MAX_BUSY_BACKOFF_S = 900
MAX_RETRY_AFTER_S = 3600
ALERT_AFTER_FAILURES = 3
QUICK_SILENCE_S = 300
QUICK_MAX_S = 120
QUICK_RECOVER_S = 1800
HEARTBEAT_STALE_S = 300
STATUS_EVERY_S = 15


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
        pattern = list(range(count)) + list(range(count - 1, -1, -1))
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
        self.answered_at: float | None = None
        self.answered_wall: str | None = None
        self.hot_months: list[tuple[int, int]] | None = None
        self.detected_at: float | None = None
        self.orders: Counter[dt.datetime | None] | None = None
        self.next_at: float | None = None

    def reset(self) -> None:
        self.known = None
        self.orders = None
        self.paused = False
        self.misses = 0
        self.next_at = None

    def due(self) -> bool:
        now = self.timer()
        if self.next_at is None:
            self.next_at = now + self.every
        if now < self.next_at:
            return False
        self.next_at = now + self.every
        return True

    def calendar_read(self) -> None:
        self.paused = False

    async def check(self, session: SiteSession) -> bool:
        if self.paused:
            return False
        base = self.settings.quick_check_seconds
        if self.every > base and self.refused_at is not None and self.timer() - self.refused_at >= QUICK_RECOVER_S:
            self.every = max(base, self.every // 2)
            self.refused_at = self.timer()
            log.info("Quick checks back to every %d s", self.every)
        today = self.clock().date()
        months = target_months(today, self.settings.months_to_check)
        last_year, last_month = months[-1]
        start = dt.date(months[0][0], months[0][1], 1)
        end = dt.date(last_year, last_month, calendar.monthrange(last_year, last_month)[1])
        try:
            orders = await session.open_orders(start, end, today)
        except SiteBusy:
            self.paused = True
            self.refused_at = self.timer()
            self.every = min(QUICK_MAX_S, self.every * 2)
            self.next_at = self.refused_at + self.every
            log.warning(
                "Quick check refused by the site (429); pausing until the next calendar read, then every %d s", self.every
            )
            return False
        if orders is None:
            self.misses += 1
            if self.misses == max(1, QUICK_SILENCE_S // max(1, self.every)):
                log.warning("Quick checks got no answer from Elisa for %d s; relying on calendar reads", QUICK_SILENCE_S)
            return False
        self.misses = 0
        self.answered_at = self.timer()
        self.answered_wall = to_iso(self.clock())
        current = Counter(orders)
        previous, self.orders, self.known = self.orders, current, len(orders)
        if previous is None:
            log.info("Quick checks running: Elisa lists %d open work order(s) in the months checked", len(orders))
            return False
        added = current - previous
        if not added:
            return False
        self.detected_at = time.monotonic()
        self.hot_months = None if None in added else sorted({(start.year, start.month) for start in added})
        log.info(
            "Elisa lists %d new open work order(s) in the months checked; reading %s now",
            sum(added.values()),
            ", ".join(f"{y}-{m:02d}" for y, m in self.hot_months) if self.hot_months else "all of them",
        )
        return True

    def live(self, session: SiteSession) -> bool:
        ready, months = session.live.take_trigger()
        if not ready:
            return False
        self.hot_months, self.detected_at = months, None
        return True

    def take_hot(self) -> tuple[list[tuple[int, int]] | None, float | None]:
        hot, detected = self.hot_months, self.detected_at
        self.hot_months, self.detected_at = None, None
        return hot, detected


class Heartbeat:
    def __init__(
        self,
        url: str | None,
        every_s: float,
        timer: Callable[[], float] = time.monotonic,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.url = url
        self.every_s = every_s
        self.timer = timer
        self.clock = clock
        self.healthy_at: float | None = None
        self.sent_at: float | None = None
        self.ok_wall: str | None = None
        self.failing = False

    def healthy(self) -> None:
        self.healthy_at = self.timer()

    async def beat(self) -> None:
        if not self.url:
            return
        now = self.timer()
        if self.healthy_at is None or now - self.healthy_at > HEARTBEAT_STALE_S:
            return
        if self.sent_at is not None and now - self.sent_at < self.every_s:
            return
        self.sent_at = now
        ok = await asyncio.to_thread(_ping, self.url)
        if not ok and not self.failing:
            log.warning("Heartbeat to the outside monitor failed")
        self.failing = not ok
        if ok and self.clock is not None:
            self.ok_wall = to_iso(self.clock())


class StatusFile:
    def __init__(self, settings: Settings, clock: Callable[[], dt.datetime], timer: Callable[[], float] = time.monotonic):
        self.path = settings.status_path
        self.clock = clock
        self.timer = timer
        self.written_at: float | None = None
        self.running_since = to_iso(clock())
        self.signed_in_at: str | None = None

    def write(self, quick: QuickCheck, heartbeat: Heartbeat, *, force: bool = False) -> None:
        now = self.timer()
        if not force and self.written_at is not None and now - self.written_at < STATUS_EVERY_S:
            return
        self.written_at = now
        data = {
            "updated_at": to_iso(self.clock()),
            "running_since": self.running_since,
            "signed_in_at": self.signed_in_at,
            "quick": {
                "every": quick.every,
                "answered_at": quick.answered_wall,
                "paused": quick.paused,
                "open_count": quick.known,
            },
            "heartbeat": {"configured": heartbeat.url is not None, "ok_at": heartbeat.ok_wall, "failing": heartbeat.failing},
        }
        try:
            write_json(self.path, data)
        except OSError as exc:
            log.debug("Could not write the status file (%s)", type(exc).__name__)


def _ping(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
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
    heartbeat = Heartbeat(settings.heartbeat_url, settings.heartbeat_seconds, clock=clock)
    status = StatusFile(settings, clock)
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
                    status.signed_in_at = None
                alerts.reset()
                heartbeat.healthy()
                await heartbeat.beat()
                status.write(quick, heartbeat)
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
                hot, detected_at = quick.take_hot() if read_all else (None, None)
                read_all = False
                report = await run_once(
                    settings,
                    selectors,
                    notifier=notifier,
                    now=clock,
                    session=session,
                    month=month,
                    only=hot,
                    detected_at=detected_at,
                )
                quick.calendar_read()
                heartbeat.healthy()
                if report.signed_in:
                    status.signed_in_at = to_iso(clock())
                status.write(quick, heartbeat)
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
            if session is None:
                status.signed_in_at = None
            can_live = session is not None and not busy and not at_least
            can_quick = can_live and settings.quick_check_seconds > 0
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
                quick_due=quick.due,
                tick=lambda: _tick(heartbeat, quick, status),
                live=(lambda: quick.live(session)) if can_live else None,
            )
    finally:
        if session is not None:
            await _shut(session, logout=settings.logout_when_off)
        log.info("Bot stopped")


async def _tick(heartbeat: Heartbeat, quick: QuickCheck, status: StatusFile) -> None:
    if quick.answered_at is not None and (heartbeat.healthy_at is None or quick.answered_at > heartbeat.healthy_at):
        heartbeat.healthy_at = quick.answered_at
    await heartbeat.beat()
    status.write(quick, heartbeat)


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
    quick_due: Callable[[], bool] | None = None,
    tick: Callable[[], Awaitable[None]] | None = None,
    live: Callable[[], bool] | None = None,
) -> bool:
    loop = asyncio.get_running_loop()
    next_quick = loop.time() + quick_every
    while not stop.is_set():
        if tick is not None:
            await tick()
        control = controls.load()
        if not control.enabled:
            return False
        if live is not None and live():
            return True
        delay = max(at_least, next_delay(failures, control.poll_seconds, busy=busy, retry_after=retry_after))
        remaining = started + delay - loop.time()
        if remaining <= 0:
            return False
        if quick is not None and remaining > 2 and (quick_due() if quick_due is not None else loop.time() >= next_quick):
            next_quick = loop.time() + quick_every
            if await quick():
                return True
            continue
        until_quick = next_quick - loop.time() if quick is not None and quick_due is None else remaining
        await _sleep(stop, max(0.05, min(1.0, remaining, until_quick)))
    return False


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
