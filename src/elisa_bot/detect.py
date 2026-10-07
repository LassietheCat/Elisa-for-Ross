from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Mapping

from .config import Settings
from .models import Slot, SlotTags
from .state import BotState


@dataclass
class Plan:
    available: set[Slot] = field(default_factory=set)
    booked: set[Slot] = field(default_factory=set)
    new: set[Slot] = field(default_factory=set)
    to_apply: list[Slot] = field(default_factory=list)
    skipped_booked: list[Slot] = field(default_factory=list)
    baseline_only: bool = False


def plan_month(
    slots: Mapping[Slot, SlotTags],
    state: BotState,
    *,
    today: dt.date,
    settings: Settings,
    already_planned: int = 0,
) -> Plan:
    available = {slot for slot, tags in slots.items() if tags.available > 0}
    plan = Plan(
        available=available,
        booked={slot for slot, tags in slots.items() if tags.booked},
        new=available - state.seen_slots(),
    )
    if not state.baseline_done and not settings.apply_on_first_run:
        plan.baseline_only = True
        return plan

    applied = state.applied_slots()
    cap = settings.max_applies_per_day
    budget = None if cap == 0 else max(0, cap - state.applies_on(today) - already_planned)
    for slot in sorted(plan.new):
        if slot.date < today or slot in applied:
            continue
        if state.attempts.get(str(slot), 0) >= settings.max_attempts_per_slot:
            continue
        if slot in plan.booked:
            plan.skipped_booked.append(slot)
            continue
        if budget is not None and len(plan.to_apply) >= budget:
            break
        plan.to_apply.append(slot)
    return plan
