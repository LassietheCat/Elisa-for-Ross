from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response

from .config import Settings
from .models import Half, MonthReading, Slot
from .site.calendar import CalendarReader
from .site.errors import InvalidCredentials, SiteError, VerificationRequired
from .site.selectors import Selectors
from .site.session import SiteSession
from .timeutil import add_months, days_in_month, now_in

log = logging.getLogger(__name__)

BOOKED = ("applied", "assigned", "confirmed")
EXPECTED_FILTERS = {"Available", "Applied", "Assigned", "Confirm"}


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SiteCheckReport:
    steps: list[Step] = field(default_factory=list)
    openings: list[Slot] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, str] = field(default_factory=dict)
    http_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps) and not self.mismatches

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))


@dataclass
class _Mark:
    schedule: int
    counts: int


class SiteData:
    def __init__(self, selectors: Selectors):
        self.sel = selectors
        self.schedule: dict[str, dict] = {}
        self.schedule_responses = 0
        self.counts: list[dict[str, int]] = []
        self.errors: list[str] = []
        self._tasks: list[asyncio.Task] = []

    def on_response(self, response: Response) -> None:
        path = urlsplit(response.url).path
        if response.status >= 400:
            self.errors.append(f"{response.status} {response.request.method} {mask_path(path)}")
        if path.endswith(self.sel.schedule_api) or path.endswith(self.sel.counts_api):
            self._tasks.append(asyncio.create_task(self._read(response, path)))

    async def _read(self, response: Response, path: str) -> None:
        try:
            data = await response.json()
        except (PlaywrightError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if path.endswith(self.sel.schedule_api):
            result = data.get("result")
            if isinstance(result, dict):
                self.schedule_responses += 1
                for day, statuses in result.items():
                    if isinstance(statuses, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)):
                        self.schedule[str(day)] = statuses
        else:
            counts = data.get("responseCount")
            if isinstance(counts, dict):
                self.counts.append({k: int(v) for k, v in counts.items() if isinstance(v, (int, float))})

    async def settle(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    def mark(self) -> _Mark:
        return _Mark(self.schedule_responses, len(self.counts))


def mask_path(path: str) -> str:
    path = re.sub(r"[0-9a-f]{12,}", "<id>", path, flags=re.I)
    return re.sub(r"\d{4,}", "<n>", path)


_BLOCKER_JS = r"""
(el) => {
  const r = el.getBoundingClientRect();
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!top || el === top || el.contains(top)) return '';
  const cls = (typeof top.className === 'string' ? top.className : '').trim().split(/\s+/).slice(0, 4).join('.');
  return top.tagName.toLowerCase() + (cls ? '.' + cls : '');
}
"""


async def diagnose(page, selectors: Selectors) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        info["page"] = mask_path(urlsplit(page.url).path)
        probes = {
            "'Open Work Orders' links (visible/all)": 'a:text-is("Open Work Orders")',
            "month title (visible/all)": selectors.month_title,
            "status filter buttons (visible/all)": selectors.status_button,
            "calendar day cells (visible/all)": selectors.day_cell,
            "open windows (visible/all)": selectors.dialog,
        }
        for label, css in probes.items():
            items = page.locator(css)
            total = await items.count()
            visible = 0
            for i in range(total):
                if await items.nth(i).is_visible():
                    visible += 1
            info[label] = f"{visible}/{total}"
        tabs = page.locator(selectors.open_work_orders_tab)
        if await tabs.count():
            blocker = await tabs.first.evaluate(_BLOCKER_JS)
            info["element on top of the tab"] = blocker or "none (the tab is clickable)"
    except PlaywrightError as exc:
        info["diagnostics stopped"] = type(exc).__name__
    return info


def compare_with_site(
    reading: MonthReading, schedule: dict[str, dict], status_of: Callable[[str], str | None]
) -> tuple[list[str], set[str]]:
    problems: list[str] = []
    unknown: set[str] = set()
    for day in range(1, days_in_month(reading.year, reading.month) + 1):
        date = dt.date(reading.year, reading.month, day)
        site = {(half, status): 0 for half in Half for status in BOOKED}
        for name, halves in schedule.get(date.isoformat(), {}).items():
            status = status_of(str(name))
            if status not in BOOKED or not isinstance(halves, dict):
                unknown.add(str(name))
                continue
            for half in Half:
                site[(half, status)] += int(halves.get(half.value.lower(), 0) or 0)
        for half in Half:
            slot = Slot(date, half)
            tags = reading.slots.get(slot)
            for status in BOOKED:
                shown = getattr(tags, status) if tags else 0
                expected = site[(half, status)]
                if shown != expected:
                    problems.append(f"{slot}: the site's data says {expected} {status}, the calendar shows {shown}")
    return problems, unknown


async def run_site_check(settings: Settings, selectors: Selectors, *, now: Callable[[], dt.datetime] | None = None) -> SiteCheckReport:
    clock = now or (lambda: now_in(settings.timezone))
    today = clock().date()
    months = [(today.year, today.month), add_months(today.year, today.month, 1)]
    report = SiteCheckReport()
    session = SiteSession(settings, selectors)
    data = SiteData(selectors)
    loop = asyncio.get_running_loop()
    step = "Start browser"
    await session.start()
    session.page.on("response", data.on_response)
    try:
        step = "Sign-in"
        started = loop.time()
        await session.login()
        report.add(step, True, f"{loop.time() - started:.1f} s")

        step = "Open Work Orders tab"
        await session.open_work_orders()
        report.add(step, True)

        step = "Status filter buttons"
        labels = await _filter_labels(session, selectors)
        report.add(step, EXPECTED_FILTERS <= set(labels), ", ".join(labels) or "none found")

        step = "Calendar reload (Available)"
        reader = CalendarReader(session.page, selectors)
        mark = data.mark()
        started = loop.time()
        await reader.refresh()
        detail = f"{loop.time() - started:.1f} s, loading " + ("detected" if reader.saw_loading else "not detected")
        report.add(step, True, detail)
        if not reader.saw_loading:
            report.warnings.append(
                "The reload finished without the status buttons being disabled, so the bot can't see when "
                "a reload is over. Check status_button in selectors.py."
            )

        for index, (year, month) in enumerate(months):
            step = f"{calendar.month_name[month]} {year}"
            if index:
                mark = data.mark()
            await reader.go_to_month(year, month)
            reading = await reader.read_displayed_month()
            raw = await reader.wait_ready()
            await data.settle()
            _check_month(report, reader, reading, raw, data, mark, step)
    except (InvalidCredentials, VerificationRequired):
        raise
    except SiteError as exc:
        report.add(step, False, f"{exc.code}: {exc}")
        report.diagnostics = await diagnose(session.page, selectors)
    except PlaywrightError as exc:
        report.add(step, False, f"browser error ({type(exc).__name__})")
        report.diagnostics = await diagnose(session.page, selectors)
    finally:
        signed_out = await session.logout()
        if signed_out:
            report.add("Sign-out", True)
        elif session.started and report.steps and report.steps[0].ok:
            report.warnings.append("Sign-out did not complete; the session will expire on its own.")
        await data.settle()
        report.http_errors = list(data.errors)
        await session.close()
    return report


async def _filter_labels(session: SiteSession, selectors: Selectors) -> list[str]:
    buttons = session.page.locator(selectors.status_button).filter(visible=True)
    labels = []
    for i in range(await buttons.count()):
        text = re.sub(r"\s+", " ", await buttons.nth(i).inner_text()).strip()
        labels.append(re.sub(r"\s*\d+$", "", text))
    return labels


def _check_month(
    report: SiteCheckReport,
    reader: CalendarReader,
    reading: MonthReading,
    raw: dict,
    data: SiteData,
    mark: _Mark,
    name: str,
) -> None:
    days = days_in_month(reading.year, reading.month)
    cells = raw.get("cells") or []
    missing = sorted(cell["day"] for cell in cells if set(cell["halves"]) != {"AM", "PM"})
    totals = {status: sum(getattr(tags, status) for tags in reading.slots.values()) for status in ("available", *BOOKED)}
    if missing:
        report.add(name, False, f"days without both AM and PM blocks: {', '.join(map(str, missing[:10]))}")
    else:
        report.add(name, True, f"{days} days; " + ", ".join(f"{count} {status}" for status, count in totals.items()))

    unknown_tags = sorted(
        {tag["word"] for cell in cells for tags in cell["halves"].values() for tag in tags if reader.status_of(tag["word"]) is None}
    )
    if unknown_tags:
        report.warnings.append(
            f"{name}: tags the bot doesn't recognise: {', '.join(unknown_tags)}. If one of them is an opening, "
            "add it to status_words['available'] in selectors.py."
        )
    report.openings += sorted(reading.available())

    if data.schedule_responses > mark.schedule:
        problems, unknown_statuses = compare_with_site(reading, data.schedule, reader.status_of)
        report.mismatches += problems
        detail = "every day matches" if not problems else f"{len(problems)} half-day(s) differ"
        report.add(f"{name}: cross-check with the site's data", not problems, detail)
        if unknown_statuses:
            report.warnings.append(f"{name}: the site's data has statuses the bot doesn't know: {', '.join(sorted(unknown_statuses))}.")
    else:
        report.warnings.append(f"{name}: the site's schedule data wasn't seen, so the cross-check was skipped.")

    if len(data.counts) > mark.counts:
        site_new = data.counts[-1].get("new")
        if site_new is not None and site_new != totals["available"]:
            report.warnings.append(
                f"{name}: the site's own count shows {site_new} open work order(s), the calendar shows "
                f"{totals['available']}. The count may cover a different date range; worth a look."
            )
