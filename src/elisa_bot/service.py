from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Callable

from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .cycle import run_once
from .notify import Notifier, build_notifier, paused_message
from .site.errors import InvalidCredentials, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SiteSession
from .state import ControlStore
from .timeutil import now_in

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 300


def next_delay(failures: int, poll_seconds: int) -> int:
    if failures <= 0:
        return poll_seconds
    return min(MAX_BACKOFF_S, poll_seconds * 2 ** min(failures, 5))


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
    loop = asyncio.get_running_loop()
    session: SiteSession | None = None
    failures = 0
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
                failures = 0
                await _sleep(stop, 1.0)
                continue
            if switched_on is not True:
                log.info("Switched on; checking every %d s", control.poll_seconds)
                switched_on = True
            if session is None:
                session = SiteSession(settings, selectors)
                await session.start()
            started = loop.time()
            try:
                await run_once(settings, selectors, notifier=notifier, now=clock, session=session)
                failures = 0
            except (InvalidCredentials, VerificationRequired) as exc:
                log.warning("Elisa did not accept the sign-in (%s); switching the bot off", exc.code)
                controls.update(by="bot", now=clock(), enabled=False)
                session = await _shut(session, logout=False)
                await notifier.send(paused_message(exc.code))
                continue
            except SiteError as exc:
                failures += 1
                log.warning("Check failed (%s), %d in a row", exc.code, failures)
                session = await _recover(session, settings, selectors)
            except PlaywrightError as exc:
                failures += 1
                log.warning("Browser problem (%s), %d in a row; restarting the browser", type(exc).__name__, failures)
                session = await _shut(session, logout=False)
            except Exception:
                failures += 1
                log.exception("Unexpected error, %d in a row; restarting the browser", failures)
                session = await _shut(session, logout=False)
            await _wait_for_next(stop, controls, started, failures)
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


async def _wait_for_next(stop: asyncio.Event, controls: ControlStore, started: float, failures: int) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        control = controls.load()
        if not control.enabled:
            return
        remaining = started + next_delay(failures, control.poll_seconds) - loop.time()
        if remaining <= 0:
            return
        await _sleep(stop, min(1.0, remaining))


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
