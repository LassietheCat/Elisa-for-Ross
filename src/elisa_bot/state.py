from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import Slot
from .timeutil import to_iso

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class BotState:
    version: int = STATE_VERSION
    baseline_done: bool = False
    seen: list[str] = field(default_factory=list)
    applied: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    applies_by_day: dict[str, int] = field(default_factory=dict)
    last_run_at: str | None = None
    last_run_ok: bool | None = None
    last_error: str | None = None

    def seen_slots(self) -> set[Slot]:
        return {Slot.parse(text) for text in self.seen}

    def applied_slots(self) -> set[Slot]:
        return {Slot.parse(text) for text in self.applied}

    def applies_on(self, day: dt.date) -> int:
        return int(self.applies_by_day.get(day.isoformat(), 0))

    def record_apply(self, slot: Slot, now: dt.datetime) -> None:
        self.applied[str(slot)] = to_iso(now)
        day = now.date().isoformat()
        self.applies_by_day[day] = self.applies_by_day.get(day, 0) + 1

    def prune(self, today: dt.date, keep_days: int = 7) -> None:
        cutoff = today - dt.timedelta(days=keep_days)
        self.applied = {k: v for k, v in self.applied.items() if Slot.parse(k).date >= cutoff}
        self.attempts = {k: v for k, v in self.attempts.items() if Slot.parse(k).date >= cutoff}
        self.applies_by_day = {
            k: v for k, v in self.applies_by_day.items() if dt.date.fromisoformat(k) >= cutoff
        }


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BotState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return BotState()
        except (json.JSONDecodeError, UnicodeDecodeError):
            backup = self.path.with_name(self.path.name + ".corrupt")
            os.replace(self.path, backup)
            log.warning("%s was unreadable; moved it to %s and started fresh", self.path.name, backup.name)
            return BotState()
        if not isinstance(data, dict):
            return BotState()
        known = {f.name for f in dataclasses.fields(BotState)}
        state = BotState(**{key: value for key, value in data.items() if key in known})
        state.version = STATE_VERSION
        return state

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
