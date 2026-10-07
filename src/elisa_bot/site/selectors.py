from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError

_STATUS_WORDS = {
    "available": r"^(available|new|open)$",
    "applied": r"^(applied|pending)$",
    "assigned": r"^assigned$",
    "confirmed": r"^confirm(ed)?$",
}


@dataclass
class Selectors:
    login_path: str = "/login"
    email_input: str = "input#email, input[type=email]"
    password_input: str = "input[type=password]"
    login_button: str = "button[type=submit]"
    login_error_re: str = r"(invalid|incorrect|wrong|not match|not recognized|failed|locked|disabled)"
    verification_re: str = r"(captcha|verification code|one[- ]time|two[- ]factor|2fa|enter the code)"

    open_work_orders_tab: str = 'a:visible:text-is("Open Work Orders")'
    status_button: str = "button:has(span.small-view-btn)"
    available_filter: str = 'button:has(span.small-view-btn:text-is("Available"))'

    month_title: str = ".calender-monyh-header"
    next_month: str = ".header i.fa-angle-right"
    prev_month: str = ".header i.fa-angle-left"
    day_cell: str = "td.calendar-day"
    date_cell: str = ".date-cell"
    day_number: str = ".date"
    half_badge: str = ".time-slab-badge"
    tag: str = "span.badge:not(.time-slab-badge)"
    status_words: dict[str, str] = field(default_factory=lambda: dict(_STATUS_WORDS))

    dialog: str = ".modal.show, .modal.in, [role=dialog]"
    dialog_buttons: str = "button, a.btn, input[type=button], input[type=submit]"
    accept_button_re: str = r"^\s*(accept|apply)\s*$"
    confirm_button_re: str = r"^\s*(yes|ok|okay|confirm|accept|apply|submit|continue)\s*$"
    close_button_re: str = r"^\s*(close|cancel|×|✕|x)\s*$"
    message: str = ".toast, .alert, [role=alert], .text-danger, .alert-danger, .swal2-popup, .toast-message, .noty_body"
    accept_failure_re: str = r"(no longer available|not available|already|taken|unable|error|failed|conflict)"


def load_selectors(path: Path | None) -> Selectors:
    selectors = Selectors()
    if path is None:
        return selectors
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"SELECTORS_FILE could not be read ({type(exc).__name__})") from None
    if not isinstance(overrides, dict):
        raise ConfigError("SELECTORS_FILE must contain a JSON object")
    known = {f.name for f in dataclasses.fields(Selectors)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ConfigError(f"SELECTORS_FILE has unknown keys: {', '.join(unknown)}")
    return dataclasses.replace(selectors, **overrides)
