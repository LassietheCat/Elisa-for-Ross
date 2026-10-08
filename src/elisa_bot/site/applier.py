from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import Error as PlaywrightError

from ..models import ApplyOutcome, ApplyResult, Slot, SlotTags
from .calendar import CalendarReader
from .errors import SiteError
from .selectors import Selectors

log = logging.getLogger(__name__)

_SEEN_ATTR = "data-elisa-bot-seen"
_SAME_WINDOW_JS = "(el, other) => el === other || el.contains(other) || other.contains(el)"


class Applier:
    def __init__(self, page: Page, selectors: Selectors, reader: CalendarReader, *, test_mode: bool):
        self.page = page
        self.sel = selectors
        self.reader = reader
        self.test_mode = test_mode
        self._accept_re = re.compile(selectors.accept_button_re, re.I)
        self._confirm_re = re.compile(selectors.confirm_button_re, re.I)
        self._close_re = re.compile(selectors.close_button_re, re.I)
        self._failure_re = re.compile(selectors.accept_failure_re, re.I)

    async def apply_one(self, slot: Slot) -> ApplyResult:
        try:
            tag = await self.reader.find_available_tag(slot)
            if tag is None:
                return ApplyResult(slot, ApplyOutcome.MISSED, "gone")
            await self._mark_current_messages()
            await tag.evaluate("el => el.scrollIntoView({block: 'center'})")
            await tag.click()
            window = await self._wait_for_window()
            if window is None:
                return ApplyResult(slot, ApplyOutcome.ERROR, "no_work_order_window")
            accept = await self._find_button(window, self._accept_re)
            if accept is None:
                await self._close_windows()
                return ApplyResult(slot, ApplyOutcome.MISSED, "no_accept_button")
            if self.test_mode:
                await self._close_windows()
                return ApplyResult(slot, ApplyOutcome.TEST_MODE, "found_accept_button")
            window_el = await window.element_handle()
        except SiteError as exc:
            return ApplyResult(slot, ApplyOutcome.ERROR, exc.code)
        except PlaywrightError as exc:
            log.warning("Could not open the opening for %s (%s)", slot, type(exc).__name__)
            await self._close_windows_quietly()
            return ApplyResult(slot, ApplyOutcome.ERROR, "open_failed")

        try:
            await accept.click()
            await self._confirm_if_asked(window_el)
            site_reported_problem = await self._watch_for_failure_message()
            await self._close_windows()
            tags = await self._recheck(slot)
        except (SiteError, PlaywrightError) as exc:
            log.warning("Clicked Accept for %s but could not check the result (%s)", slot, type(exc).__name__)
            await self._close_windows_quietly()
            return ApplyResult(slot, ApplyOutcome.UNVERIFIED, "check_failed", clicked=True)

        if tags is not None and tags.booked:
            return ApplyResult(slot, ApplyOutcome.APPLIED, "", clicked=True)
        if tags is None or tags.available == 0:
            return ApplyResult(slot, ApplyOutcome.MISSED, "taken", clicked=True)
        code = "site_reported_problem" if site_reported_problem else "no_change"
        return ApplyResult(slot, ApplyOutcome.UNVERIFIED, code, clicked=True)

    async def _visible_windows(self) -> list[Locator]:
        windows = self.page.locator(self.sel.dialog)
        visible = []
        for i in range(await windows.count()):
            window = windows.nth(i)
            if await window.is_visible():
                visible.append(window)
        return visible

    async def _wait_for_window(self, timeout_s: float = 8.0) -> Locator | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            windows = await self._visible_windows()
            if windows:
                return windows[-1]
            await asyncio.sleep(0.1)
        return None

    async def _label(self, button: Locator) -> str:
        text = (await button.inner_text()).strip()
        if text:
            return text
        for attribute in ("value", "aria-label"):
            value = await button.get_attribute(attribute)
            if value:
                return value.strip()
        return ""

    async def _find_button(self, scope: Locator, pattern: re.Pattern) -> Locator | None:
        buttons = scope.locator(self.sel.dialog_buttons)
        for i in range(await buttons.count()):
            button = buttons.nth(i)
            if not (await button.is_visible() and await button.is_enabled()):
                continue
            if pattern.match(await self._label(button)):
                return button
        return None

    async def _confirm_if_asked(self, work_order_window: ElementHandle, timeout_s: float = 2.5) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            windows = await self._visible_windows()
            if not windows:
                return
            for window in reversed(windows):
                if await window.evaluate(_SAME_WINDOW_JS, work_order_window):
                    continue
                button = await self._find_button(window, self._confirm_re)
                if button is not None:
                    await button.click()
                    return
            await asyncio.sleep(0.1)

    async def _close_windows(self) -> None:
        for _ in range(3):
            if not await self._visible_windows():
                return
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.25)
            windows = await self._visible_windows()
            if not windows:
                return
            top = windows[-1]
            button = await self._find_button(top, self._close_re)
            if button is None:
                close = top.locator("button.close, [aria-label='Close'], [data-dismiss='modal'], [data-bs-dismiss='modal']")
                if await close.count():
                    button = close.first
            if button is not None:
                await button.click()
                await asyncio.sleep(0.25)

    async def _close_windows_quietly(self) -> None:
        try:
            await self._close_windows()
        except PlaywrightError:
            pass

    async def _mark_current_messages(self) -> None:
        await self.page.evaluate(
            "([sel, attr]) => document.querySelectorAll(sel).forEach(el => el.setAttribute(attr, '1'))",
            [self.sel.message, _SEEN_ATTR],
        )

    def _new_messages_selector(self) -> str:
        return ", ".join(f"{part.strip()}:not([{_SEEN_ATTR}])" for part in self.sel.message.split(","))

    async def _failure_message_shown(self) -> bool:
        messages = self.page.locator(self._new_messages_selector())
        for i in range(await messages.count()):
            message = messages.nth(i)
            try:
                if await message.is_visible() and self._failure_re.search(await message.inner_text()):
                    return True
            except PlaywrightError:
                continue
        return False

    async def _watch_for_failure_message(self, timeout_s: float = 3.0) -> bool:
        loop = asyncio.get_running_loop()
        start = loop.time()
        windows_gone_at = None
        while loop.time() - start < timeout_s:
            if await self._failure_message_shown():
                return True
            if await self._visible_windows():
                windows_gone_at = None
            elif windows_gone_at is None:
                windows_gone_at = loop.time()
            elif loop.time() - windows_gone_at > 0.8:
                return False
            await asyncio.sleep(0.1)
        return False

    async def _recheck(self, slot: Slot) -> SlotTags | None:
        await self.reader.refresh()
        await self.reader.go_to_month(slot.date.year, slot.date.month)
        reading = await self.reader.read_displayed_month()
        return reading.slots.get(slot)
