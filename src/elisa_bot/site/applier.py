from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Locator, Page, Response
from playwright.async_api import Error as PlaywrightError

from ..models import ApplyOutcome, ApplyResult, Opening, Slot, SlotTags
from .calendar import CalendarReader
from .errors import SiteError
from .selectors import Selectors

log = logging.getLogger(__name__)

ACCEPT_ANSWER_S = 10
MAX_TRIES = 3
_SEEN_ATTR = "data-elisa-bot-seen"
_SAME_WINDOW_JS = "(el, other) => el === other || el.contains(other) || other.contains(el)"
_ROW_JS = r"""(el, row) => {
  const box = el.closest(row) || el.parentElement;
  const text = box ? box.innerText : '';
  const m = text.match(/(\d{1,2}):(\d{2})\s*(AM|PM)/i);
  const types = Array.from(new Set((text.match(/\b(VRI|OPI|OSI)\b/gi) || []).map(t => t.toUpperCase())));
  return { clock: m ? `${m[1].padStart(2, '0')}:${m[2]} ${m[3].toUpperCase()}` : null, types };
}"""
_TYPES_RE = re.compile(r"\b(VRI|OPI|OSI)\b", re.I)


_GENERIC_WORDS = frozenset(
    "a about accept accepted acceptance after again already an and any apply applied are as assigned assignment at "
    "available be been by cannot can't coi completed conflict conflicting could date do does exceeded for found has "
    "have in interpreter invalid is it limit longer maximum no not of on or order please process processed refresh "
    "request schedule scheduled screen sorry the this time to try unable was with work wo you your".split()
)


def describe_refusal(message: str) -> str:
    words = re.findall(r"[A-Za-z']+|[^A-Za-z'\s]+", message)
    kept = [word.lower() if word.lower() in _GENERIC_WORDS else "…" for word in words if re.match(r"[A-Za-z']", word)]
    text = re.sub(r"(… )+…|…( …)+", "…", " ".join(kept))
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:8]
    return f"{text or 'no reason given'} [{len(message)} chars, #{digest}]"


class Applier:
    def __init__(
        self,
        page: Page,
        selectors: Selectors,
        reader: CalendarReader,
        *,
        test_mode: bool,
        service_types: frozenset[str] | None = None,
    ):
        self.page = page
        self.sel = selectors
        self.reader = reader
        self.test_mode = test_mode
        self.service_types = service_types
        self._accept_re = re.compile(selectors.accept_button_re, re.I)
        self._confirm_re = re.compile(selectors.confirm_button_re, re.I)
        self._close_re = re.compile(selectors.close_button_re, re.I)
        self._failure_re = re.compile(selectors.accept_failure_re, re.I)
        self._taken_re = re.compile(selectors.accept_taken_re, re.I)
        self._accept_api = re.compile(selectors.accept_api_re)

    async def apply_one(self, target: Opening | Slot) -> ApplyResult:
        opening = target if isinstance(target, Opening) else Opening(target)
        slot = opening.slot
        last: ApplyResult | None = None
        tries = 0
        index = 0
        while tries < MAX_TRIES:
            try:
                tags = await self.reader.find_available_tags(slot)
                if index >= len(tags):
                    break
                if index == 0:
                    await self._mark_current_messages()
                tag = tags[index]
                await tag.evaluate("el => el.scrollIntoView({block: 'center'})")
                await tag.click()
                buttons, window = await self._wait_for_accepts(opening.clock)
            except SiteError as exc:
                return ApplyResult(slot, ApplyOutcome.ERROR, exc.code)
            except PlaywrightError as exc:
                log.warning("Could not open the opening for %s (%s)", slot, type(exc).__name__)
                await self._close_windows_quietly()
                return ApplyResult(slot, ApplyOutcome.ERROR, "open_failed")
            if window is not None:
                return await self._apply_in_window(slot, window)
            index += 1
            if not buttons:
                continue
            if self.test_mode:
                return ApplyResult(slot, ApplyOutcome.TEST_MODE, "found_accept_button")
            for button in buttons:
                result = await self._accept_from_list(slot, button)
                tries += 1
                if result.outcome not in (ApplyOutcome.REFUSED, ApplyOutcome.MISSED) or tries >= MAX_TRIES:
                    return result
                last = result
        if last is not None:
            return last
        if index == 0:
            return ApplyResult(slot, ApplyOutcome.MISSED, "gone")
        return ApplyResult(slot, ApplyOutcome.ERROR, "no_accept_button")

    def _wanted(self, types: list[str]) -> bool:
        if self.service_types is None:
            return True
        found = {code.upper() for code in types}
        return bool(found) and found <= self.service_types

    async def _apply_in_window(self, slot: Slot, window: Locator) -> ApplyResult:
        try:
            if not self._wanted(_TYPES_RE.findall(await window.inner_text())):
                await self._close_windows()
                return ApplyResult(slot, ApplyOutcome.ERROR, "not_a_wanted_type")
            accept = await self._find_button(window, self._accept_re)
            if accept is None:
                await self._close_windows()
                return ApplyResult(slot, ApplyOutcome.ERROR, "no_accept_button")
            if self.test_mode:
                await self._close_windows()
                return ApplyResult(slot, ApplyOutcome.TEST_MODE, "found_accept_button")
            window_el = await window.element_handle()
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
            log.warning("Clicked Accept for %s but could not check the result (%s); looking once more", slot, type(exc).__name__)
            await self._close_windows_quietly()
            try:
                tags = await self._recheck(slot)
            except (SiteError, PlaywrightError):
                return ApplyResult(slot, ApplyOutcome.UNVERIFIED, "check_failed", clicked=True)
            site_reported_problem = False

        if tags is not None and tags.booked:
            return ApplyResult(slot, ApplyOutcome.APPLIED, "", clicked=True)
        if tags is None or tags.available == 0:
            return ApplyResult(slot, ApplyOutcome.MISSED, "taken", clicked=True)
        code = "site_reported_problem" if site_reported_problem else "no_change"
        return ApplyResult(slot, ApplyOutcome.UNVERIFIED, code, clicked=True)

    async def _accept_from_list(self, slot: Slot, accept: Locator) -> ApplyResult:
        answers: list[Response] = []

        def on_response(response: Response) -> None:
            if response.request.method == "POST" and self._accept_api.search(urlsplit(response.url).path):
                answers.append(response)

        self.page.on("response", on_response)
        try:
            await accept.click()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + ACCEPT_ANSWER_S
            confirmed = False
            while not answers and loop.time() < deadline:
                if not confirmed:
                    confirmed = await self._click_confirm(None)
                await asyncio.sleep(0.1)
        except PlaywrightError as exc:
            log.warning("Clicked Accept for %s but could not follow it (%s)", slot, type(exc).__name__)
        finally:
            self.page.remove_listener("response", on_response)
        await self._close_windows_quietly()
        if not answers:
            log.warning("Clicked Accept for %s but Elisa's answer didn't come", slot)
            return ApplyResult(slot, ApplyOutcome.UNVERIFIED, "no_answer", clicked=True)
        response = answers[0]
        try:
            data = await response.json()
        except (PlaywrightError, ValueError):
            data = None
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        if response.ok and isinstance(data, dict) and data.get("status") is True:
            return ApplyResult(slot, ApplyOutcome.APPLIED, "", clicked=True)
        if response.ok and self._taken_re.search(message):
            return ApplyResult(slot, ApplyOutcome.MISSED, "taken", clicked=True)
        if response.ok and isinstance(data, dict) and data.get("status") is False:
            log.info("Elisa refused the Accept for %s: %s", slot, describe_refusal(message))
            return ApplyResult(slot, ApplyOutcome.REFUSED, "refused", clicked=True)
        if not response.ok:
            log.warning("Elisa answered the Accept for %s with HTTP %d", slot, response.status)
        return ApplyResult(slot, ApplyOutcome.UNVERIFIED, "site_reported_problem", clicked=True)

    async def _wait_for_accepts(
        self, clock: str | None, timeout_s: float = 8.0, settle_s: float = 1.5
    ) -> tuple[list[Locator], Locator | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        listed_at = None
        while loop.time() < deadline:
            buttons = self.page.locator(self.sel.list_accept_button)
            matching = []
            visible = 0
            for i in range(await buttons.count()):
                button = buttons.nth(i)
                if not (await button.is_visible() and await button.is_enabled()):
                    continue
                visible += 1
                row = await button.evaluate(_ROW_JS, self.sel.order_row)
                if (clock is None or row["clock"] == clock) and self._wanted(row["types"]):
                    matching.append(button)
            if matching:
                return matching, None
            if visible:
                listed_at = listed_at or loop.time()
                if loop.time() - listed_at > settle_s:
                    return [], None
            windows = await self._visible_windows()
            if windows:
                return [], windows[-1]
            await asyncio.sleep(0.1)
        return [], None

    async def _click_confirm(self, work_order_window: ElementHandle | None) -> bool:
        for window in reversed(await self._visible_windows()):
            if work_order_window is not None and await window.evaluate(_SAME_WINDOW_JS, work_order_window):
                continue
            button = await self._find_button(window, self._confirm_re)
            if button is not None:
                await button.click()
                return True
        return False

    async def _visible_windows(self) -> list[Locator]:
        windows = self.page.locator(self.sel.dialog)
        visible = []
        for i in range(await windows.count()):
            window = windows.nth(i)
            if await window.is_visible():
                visible.append(window)
        return visible

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
            if not await self._visible_windows() or await self._click_confirm(work_order_window):
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
