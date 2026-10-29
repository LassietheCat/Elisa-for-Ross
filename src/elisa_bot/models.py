from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum


class Half(str, Enum):
    AM = "AM"
    PM = "PM"


_SLOT_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s+(AM|PM)\s*$", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Slot:
    date: dt.date
    half: Half

    def __str__(self) -> str:
        return f"{self.date.isoformat()} {self.half.value}"

    @classmethod
    def parse(cls, text: str) -> Slot:
        match = _SLOT_RE.match(text)
        if not match:
            raise ValueError(f"not a slot string: {text!r}")
        return cls(dt.date.fromisoformat(match.group(1)), Half(match.group(2).upper()))

    def label(self) -> str:
        return f"{self.date:%a %b} {self.date.day} {self.half.value}"


JOB_STATUSES = ("applied", "assigned", "confirmed")


@dataclass(frozen=True)
class OpenOrders:
    starts: tuple[dt.datetime, ...] = ()
    untimed_days: frozenset[dt.date] = frozenset()


@dataclass(frozen=True)
class Opening:
    slot: Slot
    start: dt.datetime | None = None

    @property
    def clock(self) -> str | None:
        return None if self.start is None else f"{self.start:%I:%M %p}"

    @classmethod
    def at(cls, start: dt.datetime) -> Opening:
        return cls(Slot(start.date(), Half.AM if start.hour < 12 else Half.PM), start)


@dataclass
class SlotTags:
    available: int = 0
    applied: int = 0
    assigned: int = 0
    confirmed: int = 0

    @property
    def booked(self) -> bool:
        return self.applied > 0 or self.assigned > 0 or self.confirmed > 0

    def jobs(self) -> dict[str, int]:
        return {status: getattr(self, status) for status in JOB_STATUSES if getattr(self, status)}


@dataclass
class MonthReading:
    year: int
    month: int
    slots: dict[Slot, SlotTags] = field(default_factory=dict)
    unread: dict[dt.date, int] = field(default_factory=dict)
    unknown_words: set[str] = field(default_factory=set)

    def available(self) -> set[Slot]:
        return {slot for slot, tags in self.slots.items() if tags.available > 0}


class ApplyOutcome(str, Enum):
    APPLIED = "applied"
    UNVERIFIED = "unverified"
    MISSED = "missed"
    REFUSED = "refused"
    ERROR = "error"
    TEST_MODE = "test_mode"


@dataclass(frozen=True)
class ApplyResult:
    slot: Slot
    outcome: ApplyOutcome
    code: str = ""
    clicked: bool = False
