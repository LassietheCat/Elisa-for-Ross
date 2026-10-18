from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import math
import re
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Request, Response, async_playwright
from playwright.async_api import Error as PlaywrightError

from ..config import Settings
from .errors import (
    DataRequestFailed,
    InvalidCredentials,
    LoginFailed,
    NavigationFailed,
    SignInPaused,
    SiteBusy,
    SiteError,
    VerificationRequired,
)
from .selectors import Selectors

log = logging.getLogger(__name__)

LOGIN_TIMEOUT_S = 30
SIGN_IN_WINDOW_S = 3600
_WATCHED_TYPES = ("document", "xhr", "fetch")
_NOT_REPLAYED = {"content-length", "cookie", "host"}


def _date_range(body: dict) -> dict | None:
    for value in (body, *body.values()):
        if isinstance(value, dict) and "startDate" in value and "endDate" in value:
            return value
    return None


class SignInLimiter:
    def __init__(self, per_hour: int, *, clock: Callable[[], float] = time.monotonic):
        self.per_hour = per_hour
        self.clock = clock
        self._attempts: deque[float] = deque()

    def take(self) -> None:
        now = self.clock()
        while self._attempts and now - self._attempts[0] >= SIGN_IN_WINDOW_S:
            self._attempts.popleft()
        if len(self._attempts) >= self.per_hour:
            wait = math.ceil(self._attempts[0] + SIGN_IN_WINDOW_S - now)
            raise SignInPaused(f"already signed in {self.per_hour} times in the last hour", retry_after=wait)
        self._attempts.append(now)


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0, math.ceil((when - dt.datetime.now(dt.timezone.utc)).total_seconds()))


class SiteSession:
    def __init__(self, settings: Settings, selectors: Selectors, *, sign_ins: SignInLimiter | None = None):
        self.settings = settings
        self.sel = selectors
        self.sign_ins = sign_ins
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._problem: SiteError | None = None
        self._open_counts: list[int] = []
        self._count_reads: list[asyncio.Future] = []
        self._warned: set[tuple] = set()
        self._count_request: tuple[str, dict] | None = None
        self._api_headers: dict[str, str] | None = None

    @property
    def started(self) -> bool:
        return self._page is not None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("session not started")
        return self._page

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.settings.headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            timezone_id=self.settings.timezone,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(20_000)
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)

    def _on_request(self, request: Request) -> None:
        if request.method != "POST" or request.resource_type not in _WATCHED_TYPES:
            return
        path = urlsplit(request.url).path
        if not (path.endswith(self.sel.counts_api) or path.endswith(self.sel.schedule_api)):
            return
        if path.endswith(self.sel.counts_api):
            try:
                body = request.post_data_json
            except (PlaywrightError, ValueError):
                body = None
            if isinstance(body, dict) and _date_range(body) is not None:
                self._count_request = (request.url, body)
        asyncio.ensure_future(self._remember_headers(request))

    async def _remember_headers(self, request: Request) -> None:
        try:
            headers = await request.all_headers()
        except PlaywrightError:
            return
        self._api_headers = {
            name: value for name, value in headers.items() if not name.startswith(":") and name.lower() not in _NOT_REPLAYED
        }

    async def count_open(self, start: dt.date, end: dt.date, today: dt.date) -> int | None:
        if self._page is None or self._count_request is None or self._api_headers is None:
            return None
        url, template = self._count_request
        body = copy.deepcopy(template)
        dates = _date_range(body)
        dates["startDate"], dates["endDate"] = start.isoformat(), end.isoformat()
        if "currentDate" in dates:
            dates["currentDate"] = today.isoformat()
        try:
            response = await self._page.context.request.post(
                url, headers=self._api_headers, data=json.dumps(body), timeout=10_000
            )
        except PlaywrightError:
            return None
        if response.status == 429:
            raise SiteBusy("the site answered 429 Too Many Requests", retry_after=parse_retry_after(response.headers.get("retry-after")))
        if response.status in (401, 403):
            self._api_headers = None
            return None
        if not response.ok:
            return None
        try:
            data = await response.json()
        except (PlaywrightError, ValueError):
            return None
        counts = data.get("responseCount") if isinstance(data, dict) else None
        value = counts.get("new") if isinstance(counts, dict) else None
        return value if type(value) is int else None

    def _on_response(self, response: Response) -> None:
        if response.request.resource_type not in _WATCHED_TYPES:
            return
        path = urlsplit(response.url).path
        if response.ok and path.endswith(self.sel.counts_api):
            self._count_reads.append(asyncio.ensure_future(self._read_open_count(response)))
        if isinstance(self._problem, SiteBusy):
            return
        if response.status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self._problem = SiteBusy("the site answered 429 Too Many Requests", retry_after=retry_after)
        elif self._problem is None and response.status >= 400 and (
            path.endswith(self.sel.schedule_api) or path.endswith(self.sel.counts_api)
        ):
            self._problem = DataRequestFailed(f"a calendar data request failed ({response.status})")

    async def _read_open_count(self, response: Response) -> None:
        try:
            data = await response.json()
        except (PlaywrightError, ValueError):
            return
        counts = data.get("responseCount") if isinstance(data, dict) else None
        if isinstance(counts, dict) and type(counts.get("new")) is int:
            self._open_counts.append(counts["new"])
            if ("open count",) not in self._warned:
                self._warned.add(("open count",))
                log.info("Reading Elisa's own open-work-order count too (now %d)", counts["new"])

    async def site_open_count(self) -> int | None:
        reads, self._count_reads = self._count_reads, []
        if reads:
            await asyncio.gather(*reads, return_exceptions=True)
        return self._open_counts[-1] if self._open_counts else None

    def warn_once(self, key: tuple, message: str, *args: object) -> bool:
        if key in self._warned:
            return False
        self._warned.add(key)
        log.warning(message, *args)
        return True

    def clear_problems(self) -> None:
        self._problem = None
        self._open_counts.clear()

    def problem(self) -> SiteError | None:
        return self._problem

    def raise_for_problems(self) -> None:
        if self._problem is not None:
            raise self._problem

    async def close(self) -> None:
        for closable in (self._context, self._browser):
            if closable is not None:
                try:
                    await closable.close()
                except PlaywrightError:
                    pass
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

    def _on_login_page(self) -> bool:
        return urlsplit(self.page.url).path.startswith(self.sel.login_path)

    def is_signed_in(self) -> bool:
        return self.page.url.startswith("http") and not self._on_login_page()

    async def login(self) -> None:
        self.settings.require_site_credentials()
        if self.sign_ins is not None:
            self.sign_ins.take()
        page = self.page
        await page.goto(self.settings.base_url + self.sel.login_path, wait_until="domcontentloaded")
        email = page.locator(self.sel.email_input).first
        try:
            await email.wait_for(state="visible", timeout=30_000)
        except PlaywrightError:
            raise LoginFailed("the sign-in form did not appear") from None
        await email.fill(self.settings.elisa_email or "")
        await page.locator(self.sel.password_input).first.fill(self.settings.elisa_password or "")
        button = page.locator(self.sel.login_button).first
        if not await _wait_until(button.is_enabled, timeout_s=5):
            raise LoginFailed("the Login button stayed disabled")
        await button.click()
        await self._wait_for_sign_in()
        log.info("Signed in to Elisa")

    async def _wait_for_sign_in(self) -> None:
        verification = re.compile(self.sel.verification_re, re.I)
        rejected = re.compile(self.sel.login_error_re, re.I)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOGIN_TIMEOUT_S
        while True:
            if not self._on_login_page():
                return
            if isinstance(self._problem, SiteBusy):
                raise self._problem
            text = await self._page_text()
            if verification.search(text):
                raise VerificationRequired("the site asked for a verification step")
            if rejected.search(text):
                raise InvalidCredentials("the site rejected the email or password")
            if loop.time() > deadline:
                raise LoginFailed("sign-in did not finish in time")
            await asyncio.sleep(0.25)

    async def _page_text(self) -> str:
        try:
            return await self.page.locator("body").inner_text(timeout=2_000)
        except PlaywrightError:
            return ""

    async def open_work_orders(self) -> None:
        tab = self.page.locator(self.sel.open_work_orders_tab).first
        try:
            await tab.wait_for(state="visible", timeout=30_000)
        except PlaywrightError:
            raise NavigationFailed("the Open Work Orders tab was not found") from None
        await tab.click()
        try:
            await self._month_title().wait_for(state="visible", timeout=30_000)
        except PlaywrightError:
            raise NavigationFailed("the calendar did not appear") from None

    def _month_title(self) -> Locator:
        return self.page.locator(self.sel.month_title).filter(visible=True).first

    async def calendar_visible(self) -> bool:
        try:
            return await self.page.locator(self.sel.month_title).filter(visible=True).count() > 0
        except PlaywrightError:
            return False

    async def ensure_ready(self) -> bool:
        signed_in_now = False
        if not self.is_signed_in():
            await self.login()
            signed_in_now = True
        if not await self.calendar_visible():
            await self.open_work_orders()
        return signed_in_now

    async def logout(self) -> bool:
        if not self.started or not self.is_signed_in():
            return False
        page = self.page
        logout_text = re.compile(self.sel.logout_re, re.I)
        try:
            link = await _first_visible(page.get_by_text(logout_text))
            if link is None:
                menus = page.locator(self.sel.user_menu)
                for i in reversed(range(await menus.count())):
                    menu = menus.nth(i)
                    if await menu.is_visible():
                        await menu.click(timeout=5_000)
                        break
                await asyncio.sleep(0.5)
                link = await _first_visible(page.get_by_text(logout_text))
            if link is None:
                log.info("Sign-out link not found; the session will expire on its own")
                return False
            await link.click(timeout=5_000)
            await page.wait_for_url(
                lambda url: urlsplit(url).path.startswith(self.sel.login_path), timeout=10_000
            )
        except PlaywrightError:
            log.info("Sign-out did not complete; the session will expire on its own")
            return False
        log.info("Signed out of Elisa")
        return True


async def _first_visible(locator: Locator) -> Locator | None:
    for i in range(await locator.count()):
        item = locator.nth(i)
        if await item.is_visible():
            return item
    return None


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout_s: float, interval_s: float = 0.1
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if await predicate():
            return True
        if loop.time() > deadline:
            return False
        await asyncio.sleep(interval_s)
