from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from ..config import Settings
from .errors import InvalidCredentials, LoginFailed, NavigationFailed, VerificationRequired
from .selectors import Selectors

log = logging.getLogger(__name__)

LOGIN_TIMEOUT_S = 30


class SiteSession:
    def __init__(self, settings: Settings, selectors: Selectors):
        self.settings = settings
        self.sel = selectors
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

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
