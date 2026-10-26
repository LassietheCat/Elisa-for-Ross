from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections import deque
from typing import Callable, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..models import Half, Slot

log = logging.getLogger(__name__)

ASSIGNED_EVENT = "coi_assigned"
QUIET_EVENTS = frozenset({"pg_status_update"})
ORDER_ROOM_PREFIX = "OPEN_WO_"
TRIGGER_GAP_S = 10
TRIGGERS_PER_HOUR = 30
SEEN_KEPT_S = 3600
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def parse_event(frame: object) -> tuple[str, list] | None:
    if not isinstance(frame, str) or not frame.startswith("42"):
        return None
    body = frame[2:]
    if body.startswith("/"):
        comma = body.find(",")
        if comma < 0:
            return None
        body = body[comma + 1 :]
    try:
        packet = json.loads(body.lstrip("0123456789"))
    except ValueError:
        return None
    if not isinstance(packet, list) or not packet or not isinstance(packet[0], str):
        return None
    return packet[0], packet[1:]


def event_slot(args: list, tz: ZoneInfo) -> tuple[dt.date | None, Half | None]:
    data = args[0] if args and isinstance(args[0], dict) else {}
    day = _utc_day(data.get("deldate"))
    if day is None:
        return None, None
    moment = start_time(data, tz)
    if moment is None:
        return day, None
    return moment.date(), Half.AM if moment.hour < 12 else Half.PM


def start_time(data: dict, tz: ZoneInfo) -> dt.datetime | None:
    day = _utc_day(data.get("deldate"))
    if day is None:
        return None
    return _viewer_time(day, data.get("sched"), data.get("courtTimezone"), tz)


def _utc_day(value: object) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        if len(value) == 10:
            return dt.date.fromisoformat(value)
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc)
    return moment.date()


def _viewer_time(day: dt.date, sched: object, court_tz: object, tz: ZoneInfo) -> dt.datetime | None:
    if not isinstance(sched, str) or not isinstance(court_tz, str):
        return None
    match = _CLOCK_RE.match(sched)
    if not match:
        return None
    try:
        clock = dt.time(int(match.group(1)), int(match.group(2)))
        zone = ZoneInfo(court_tz)
    except (ValueError, KeyError):
        return None
    return dt.datetime.combine(day, clock, tzinfo=zone).astimezone(tz)


def _label(day: dt.date | None, half: Half | None) -> str:
    if day is None:
        return "an unknown date"
    if half is None:
        return f"{day:%a %b} {day.day}"
    return Slot(day, half).label()


class LiveChannel:
    def __init__(self, tz: ZoneInfo, timer: Callable[[], float] = time.monotonic):
        self.tz = tz
        self.timer = timer
        self.counts: dict[str, int] = {}
        self.order_rooms = 0
        self._pending: list[tuple[int, int] | None] = []
        self._last_trigger: float | None = None
        self._triggers: dict[str, deque[float]] = {}
        self._capped: set[str] = set()
        self._seen_open: dict[Slot, float] = {}

    def connected(self, url: str) -> None:
        parts = urlsplit(url)
        log.info("Live channel connected (%s%s)", parts.netloc, parts.path)

    def closed(self, *_: object) -> None:
        log.info("Live channel closed")

    def sent(self, frame: object) -> None:
        event = parse_event(frame)
        if event is None or event[0] != "subscribe":
            return
        room = event[1][0] if event[1] and isinstance(event[1][0], str) else ""
        if room.startswith(ORDER_ROOM_PREFIX):
            self.order_rooms += 1
            log.info("Live channel: the page joined a work-order room (%d so far)", self.order_rooms)

    def received(self, frame: object) -> None:
        event = parse_event(frame)
        if event is None:
            return
        name, args = event
        now = self.timer()
        first = name not in self.counts
        self.counts[name] = self.counts.get(name, 0) + 1
        if name in QUIET_EVENTS:
            if first:
                log.info("Live channel: first '%s' event", name)
            return
        day, half = event_slot(args, self.tz)
        when = _label(day, half)
        if name == ASSIGNED_EVENT:
            log.info("Live channel: a work order for %s was assigned; %s", when, self._seen_text(day, half, now))
            return
        recent = self._triggers.setdefault(name, deque())
        while recent and now - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= TRIGGERS_PER_HOUR:
            if name not in self._capped:
                self._capped.add(name)
                log.warning("Live channel: over %d '%s' events in an hour; not reacting to more of them for now", TRIGGERS_PER_HOUR, name)
            return
        self._capped.discard(name)
        recent.append(now)
        self._pending.append((day.year, day.month) if day else None)
        log.info("Live channel: '%s' event for %s; reading that month now", name, when)

    def take_trigger(self) -> tuple[bool, list[tuple[int, int]] | None]:
        if not self._pending:
            return False, None
        now = self.timer()
        if self._last_trigger is not None and now - self._last_trigger < TRIGGER_GAP_S:
            return False, None
        pending, self._pending = self._pending, []
        self._last_trigger = now
        if any(month is None for month in pending):
            return True, None
        return True, sorted(set(pending))

    def saw_open(self, slots: Iterable[Slot]) -> None:
        now = self.timer()
        for slot in slots:
            self._seen_open[slot] = now
        self._seen_open = {slot: at for slot, at in self._seen_open.items() if now - at <= SEEN_KEPT_S}

    def _seen_text(self, day: dt.date | None, half: Half | None, now: float) -> str:
        times = [
            at
            for slot, at in self._seen_open.items()
            if slot.date == day and (half is None or slot.half == half) and now - at <= SEEN_KEPT_S
        ]
        if not times:
            return "the bot had not seen it open"
        return f"the bot saw it open {round(now - max(times))} s before"
