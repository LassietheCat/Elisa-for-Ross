from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Callable

from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .cycle import run_once
from .notify import Notifier, build_notifier, paused_message, recovered_message, trouble_message
from .site.errors import InvalidCredentials, SignInPaused, SiteBusy, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SignInLimiter, SiteSession
from .state import ControlStore
from .timeutil import now_in

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 300
MAX_BUSY_BACKOFF_S = 900
MAX_RETRY_AFTER_S = 3600
ALERT_AFTER_FAILURES = 3


def next_delay(failures: int, poll_seconds: int, *, busy: bool = False, retry_after: int | None = None) -> int:
    if failures <= 0:
        return poll_seconds
    if not busy:
        return min(MAX_BACKOFF_S, poll_seconds * 2 ** min(failures, 5))
    delay = min(MAX_BUSY_BACKOFF_S, poll_seconds * 2 ** min(failures + 1, 8))
    return max(delay, min(retry_after or 0, MAX_RETRY_AFTER_S))


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
                log.info("Switched on; checking every %d s", control.poll_seconds)
                switched_on = True
            started = loop.time()
            busy = False
            retry_after = None
            try:
                if session is None:
                    session = SiteSession(settings, selectors, sign_ins=sign_ins)
                    await session.start()
                await run_once(settings, selectors, notifier=notifier, now=clock, session=session)
                await alerts.succeeded()
            except (InvalidCredentials, VerificationRequired) as exc:
                log.warning("Elisa did not accept the sign-in (%s); switching the bot off", exc.code)
                controls.update(by="bot", now=clock(), enabled=False)
                session = await _shut(session, logout=False)
                await notifier.send(paused_message(exc.code))
                continue
            except (SiteBusy, SignInPaused) as exc:
                busy, retry_after = True, exc.retry_after
                await alerts.failed(exc.code)
                log.warning("Check failed (%s), %d in a row; waiting longer", exc.code, alerts.failures)
            except SiteError as exc:
                await alerts.failed(exc.code)
                log.warning("Check failed (%s), %d in a row", exc.code, alerts.failures)
                session = await _recover(session, settings, selectors)
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
            await _wait_for_next(stop, controls, started, alerts.failures, busy=busy, retry_after=retry_after)
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
) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        control = controls.load()
        if not control.enabled:
            return
        delay = next_delay(failures, control.poll_seconds, busy=busy, retry_after=retry_after)
        remaining = started + delay - loop.time()
        if remaining <= 0:
            return
        await _sleep(stop, min(1.0, remaining))


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
