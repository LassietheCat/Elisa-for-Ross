from __future__ import annotations

import asyncio
import calendar as _calendar
import datetime as dt
import logging
import re
from typing import Awaitable, Callable, Iterable

from playwright.async_api import ElementHandle, Page
from playwright.async_api import Error as PlaywrightError

from ..models import Half, MonthReading, Slot, SlotTags
from ..timeutil import days_in_month
from .errors import CalendarNotRendered, NavigationFailed
from .selectors import Selectors

log = logging.getLogger(__name__)

_MONTHS = {name.lower(): number for number, name in enumerate(_calendar.month_name) if name}
_TITLE_RE = re.compile(r"^\s*([A-Za-z]+)\s+(\d{4})\s*$")

_WALK_JS = r"""
  const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const shown = el => el.getClientRects().length > 0;
  const TAG = /^(\d+)\s+([A-Za-z][A-Za-z ]*)$/;
  function* walk() {
    for (const td of document.querySelectorAll(sel.dayCell)) {
      if (!shown(td)) continue;
      const cell = td.querySelector(sel.dateCell);
      if (!cell) continue;
      const dayEl = cell.querySelector(sel.dayNumber);
      const day = dayEl ? parseInt(txt(dayEl), 10) : NaN;
      if (!Number.isInteger(day)) continue;
      const halves = {};
      for (const pill of cell.querySelectorAll(sel.halfBadge)) {
        const half = txt(pill).toUpperCase();
        if (half !== 'AM' && half !== 'PM') continue;
        let block = pill;
        while (block.parentElement && block.parentElement !== cell) block = block.parentElement;
        const tags = [];
        for (const el of block.querySelectorAll(sel.tag)) {
          const m = txt(el).match(TAG);
          if (m) tags.push({ count: parseInt(m[1], 10), word: m[2].trim(), el });
        }
        halves[half] = tags;
      }
      yield { day, halves };
    }
  }
"""

_READ_JS = (
    "(sel) => {"
    + _WALK_JS
    + r"""
  const title = Array.from(document.querySelectorAll(sel.monthTitle)).find(shown) || null;
  const cells = [];
  for (const { day, halves } of walk()) {
    const plain = {};
    for (const [half, tags] of Object.entries(halves)) plain[half] = tags.map(({ count, word }) => ({ count, word }));
    cells.push({ day, halves: plain });
  }
  return { title: title ? txt(title) : null, cells };
}"""
)

_FIND_TAG_JS = (
    "(sel) => {"
    + _WALK_JS
    + r"""
  const available = new RegExp(sel.availableRe, 'i');
  for (const { day, halves } of walk()) {
    if (day !== sel.day) continue;
    for (const tag of halves[sel.half] || []) {
      if (available.test(tag.word)) return tag.el;
    }
  }
  return null;
}"""
)


def parse_month_title(title: str | None) -> tuple[int, int] | None:
    if not title:
        return None
    match = _TITLE_RE.match(title)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    return (int(match.group(2)), month) if month else None


class CalendarReader:
    def __init__(self, page: Page, selectors: Selectors, *, settle_ms: int = 300, timeout_ms: int = 15_000):
        self.page = page
        self.sel = selectors
        self.settle_s = settle_ms / 1000
        self.timeout_s = timeout_ms / 1000
        self.saw_loading = False
        self._status_res = {status: re.compile(rx, re.I) for status, rx in selectors.status_words.items()}

    def _js_args(self, **extra) -> dict:
        s = self.sel
        return {
            "monthTitle": s.month_title,
            "dayCell": s.day_cell,
            "dateCell": s.date_cell,
            "dayNumber": s.day_number,
            "halfBadge": s.half_badge,
            "tag": s.tag,
            **extra,
        }

    async def _raw(self) -> dict:
        return await self.page.evaluate(_READ_JS, self._js_args())

    async def _busy(self) -> bool:
        buttons = self.page.locator(self.sel.status_button).filter(visible=True)
        for i in range(await buttons.count()):
            if await buttons.nth(i).is_disabled():
                return True
        return False

    async def _idle(self) -> bool:
        return not await self._busy()

    async def _wait(self, predicate: Callable[[], Awaitable[bool]], what: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while not await predicate():
            if loop.time() > deadline:
                raise CalendarNotRendered(f"timed out waiting for {what}")
            await asyncio.sleep(0.05)

    async def wait_ready(self) -> dict:
        await self._wait(self._idle, "the calendar to finish loading")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        previous = await self._raw()
        while True:
            await asyncio.sleep(self.settle_s)
            current = await self._raw()
            if current == previous and await self._idle():
                return current
            if loop.time() > deadline:
                raise CalendarNotRendered("the calendar kept changing")
            previous = current

    async def refresh(self) -> tuple[int, int]:
        button = self.page.locator(self.sel.available_filter).filter(visible=True).first
        try:
            await button.wait_for(state="visible", timeout=int(self.timeout_s * 1000))
        except PlaywrightError:
            raise NavigationFailed("the Available filter button was not found") from None
        await self._wait(self._idle, "the calendar to be idle")
        await button.click()
        loop = asyncio.get_running_loop()
        started_by = loop.time() + 1.5
        self.saw_loading = False
        while loop.time() < started_by:
            if await self._busy():
                self.saw_loading = True
                break
            await asyncio.sleep(0.025)
        title = (await self.wait_ready()).get("title")
        month = parse_month_title(title)
        if month is None:
            raise CalendarNotRendered("the month title is missing after the reload")
        return month

    async def displayed_month(self) -> tuple[int, int]:
        month = parse_month_title((await self._raw()).get("title"))
        if month is None:
            raise CalendarNotRendered("the month title is missing")
        return month

    async def go_to_month(self, year: int, month: int) -> None:
        for _ in range(14):
            current = await self.displayed_month()
            if current == (year, month):
                return
            arrow = self.sel.next_month if (year, month) > current else self.sel.prev_month
            await self._wait(self._idle, "the calendar to be idle")
            try:
                await self.page.locator(arrow).filter(visible=True).first.click()
            except PlaywrightError:
                raise NavigationFailed("the month arrow was not found") from None

            async def moved(start: tuple[int, int] = current) -> bool:
                return parse_month_title((await self._raw()).get("title")) not in (None, start)

            await self._wait(moved, "the month to change")
            await self.wait_ready()
        raise NavigationFailed(f"could not reach {year}-{month:02d}")

    async def read_displayed_month(self) -> MonthReading:
        raw = await self.wait_ready()
        reading = self._parse(raw)
        if reading is not None:
            return reading
        target = parse_month_title(raw.get("title"))
        log.info("The calendar looked incomplete; reloading once")
        await self.refresh()
        if target is not None:
            await self.go_to_month(*target)
        reading = self._parse(await self.wait_ready())
        if reading is None:
            raise CalendarNotRendered("the calendar was still incomplete after one reload")
        return reading

    async def read_months(self, months: Iterable[tuple[int, int]]) -> dict[Slot, SlotTags]:
        slots: dict[Slot, SlotTags] = {}
        for year, month in months:
            await self.go_to_month(year, month)
            slots.update((await self.read_displayed_month()).slots)
        return slots

    async def find_available_tag(self, slot: Slot) -> ElementHandle | None:
        await self.go_to_month(slot.date.year, slot.date.month)
        handle = await self.page.evaluate_handle(
            _FIND_TAG_JS,
            self._js_args(day=slot.date.day, half=slot.half.value, availableRe=self.sel.status_words["available"]),
        )
        element = handle.as_element()
        if element is None:
            await handle.dispose()
        return element

    def status_of(self, word: str) -> str | None:
        for status, regex in self._status_res.items():
            if regex.match(word.strip()):
                return status
        return None

    def _parse(self, raw: dict) -> MonthReading | None:
        month_of = parse_month_title(raw.get("title"))
        if month_of is None:
            return None
        year, month = month_of
        cells = raw.get("cells") or []
        if sorted(cell["day"] for cell in cells) != list(range(1, days_in_month(year, month) + 1)):
            return None
        slots: dict[Slot, SlotTags] = {}
        for cell in cells:
            day = dt.date(year, month, cell["day"])
            for half in Half:
                tags = SlotTags()
                for tag in cell["halves"].get(half.value, []):
                    status = self.status_of(tag["word"])
                    if status is None:
                        log.debug("Ignoring a tag with an unknown status word on %s %s", day, half.value)
                        continue
                    setattr(tags, status, getattr(tags, status) + int(tag["count"]))
                slots[Slot(day, half)] = tags
        return MonthReading(year, month, slots)
