from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import Settings
from .models import Slot
from .notify import Notifier, build_notifier, test_message
from .state import MAX_JOBS_PER_DAY, BotState, ControlState, ControlStore, StateStore
from .timeutil import now_in

COOKIE = "elisa_control"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_LIMIT = 5
LOGIN_WINDOW_S = 15 * 60
INTERVAL_CHOICES = (15, 20, 30, 60, 120, 300, 600)
ACTIVITY_SHOWN = 10
STATIC = Path(__file__).parent / "static"
ICON_SIZES = (180, 192, 512)
MANIFEST = {
    "name": "Elisa bot",
    "short_name": "Elisa bot",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f5f5f7",
    "theme_color": "#1a7f37",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}
STATUS_STALE_S = 120
OUTCOMES = {
    "applied": "accepted",
    "missed": "taken by someone else first",
    "unverified": "Accept clicked, please check Elisa",
    "error": "found, but Accept couldn't be tried",
    "test_mode": "found (test mode, not clicked)",
    "skipped": "skipped, already booked then",
    "day off": "skipped, day off",
    "too close": "skipped, too close to another job",
    "day full": "skipped, enough jobs that day",
    "gone": "a new opening was gone before the bot could read it",
}
FLASH = {
    "sent": "Test alert sent. Check your inbox.",
    "failed": "The test alert could not be sent. Check the email settings.",
    "saved": "Saved.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; manifest-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

LAYOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#1a7f37">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Elisa bot">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
%%REFRESH%%<title>Elisa bot</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--on:#1a7f37;--stop:#c62828;
--warn:#a35200;--border:#d2d2d7}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--card:#1c1c1e;--text:#f2f2f7;--muted:#a1a1a6;--on:#30d158;
--stop:#ff453a;--warn:#ff9f0a;--border:#3a3a3c}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:480px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:6px 0 14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:12px}
.state{font-size:30px;font-weight:700;margin:0}
.state.on{color:var(--on)} .state.off{color:var(--muted)}
p{margin:6px 0} .muted{color:var(--muted);font-size:14px} .warn{color:var(--warn);font-weight:600}
.flash{border-color:var(--on)}
button{font:inherit;border:0;border-radius:12px;padding:12px 16px;cursor:pointer}
.big{width:100%;font-size:20px;font-weight:700;color:#fff;padding:18px;margin-top:12px}
.big.turn-on{background:var(--on)} .big.turn-off{background:var(--stop)}
.secondary{background:transparent;border:1px solid var(--border);color:var(--text)}
select,input[type=password],input[type=date]{font:inherit;padding:10px;border-radius:10px;
border:1px solid var(--border);background:var(--card);color:var(--text)}
.small{padding:6px 10px;font-size:14px}
ul.plain{list-style:none;padding-left:0} ul.plain li{display:flex;justify-content:space-between;align-items:center;
gap:8px;margin:4px 0}
input[type=password]{width:100%;box-sizing:border-box;margin:8px 0 12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
ul{margin:6px 0 0;padding-left:20px}
</style></head>
<body><main>%%BODY%%</main></body></html>"""


def _page(body: str, *, status: int = 200, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">\n' if refresh else ""
    return HTMLResponse(LAYOUT.replace("%%REFRESH%%", meta).replace("%%BODY%%", body), status_code=status)


def _login_page(message: str = "") -> str:
    note = f'<p class="warn">{html.escape(message)}</p>' if message else ""
    return (
        "<h1>Elisa bot</h1>"
        '<form class="card" method="post" action="/login">'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>'
        f"{note}"
        '<button class="big turn-on" type="submit">Sign in</button>'
        "</form>"
    )


def _when(text: str | None, tz: str) -> str:
    if not text:
        return "never"
    try:
        moment = dt.datetime.fromisoformat(text).astimezone(ZoneInfo(tz))
    except ValueError:
        return "unknown"
    return f"{moment:%a %b} {moment.day}, {moment.hour % 12 or 12}:{moment:%M:%S} {moment:%p}"


def _read_status(settings: Settings) -> dict:
    try:
        data = json.loads(settings.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _age(text: str | None, now: dt.datetime) -> float | None:
    if not text:
        return None
    try:
        return (now - dt.datetime.fromisoformat(text)).total_seconds()
    except (TypeError, ValueError):
        return None


def _right_now(settings: Settings, control: ControlState, state: BotState, status: dict, now: dt.datetime) -> str:
    tz = settings.timezone
    lines = []
    if control.enabled:
        quick = status.get("quick") if isinstance(status.get("quick"), dict) else {}
        if not settings.quick_check_seconds:
            pass
        elif quick.get("paused"):
            lines.append("<p>Quick checks: paused after Elisa refused one; they restart after the next calendar read.</p>")
        elif quick.get("answered_at"):
            count = quick.get("open_count")
            listed = f" Elisa lists {count} open order{'' if count == 1 else 's'}." if isinstance(count, int) else ""
            lines.append(
                f"<p>Quick checks: every {html.escape(str(quick.get('every')))} seconds, "
                f"last answer {_when(quick['answered_at'], tz)}.{listed}</p>"
            )
        else:
            lines.append("<p>Quick checks: starting.</p>")
        if status.get("signed_in_at"):
            lines.append(f"<p>Signed in to Elisa since {_when(status['signed_in_at'], tz)}.</p>")
        age = _age(status.get("updated_at"), now)
        if age is not None and age > STATUS_STALE_S:
            lines.append(f'<p class="warn">The bot hasn\'t reported for {int(age // 60)} min. It may not be running.</p>')
    beat = status.get("heartbeat") if isinstance(status.get("heartbeat"), dict) else {}
    if not beat.get("configured"):
        lines.append('<p class="muted">Outside alarm: not set up.</p>')
    elif beat.get("failing"):
        lines.append('<p class="warn">Outside alarm: the last signal failed.</p>')
    elif beat.get("ok_at"):
        lines.append(f"<p>Outside alarm: on, last signal {_when(beat['ok_at'], tz)}.</p>")
    today = now.date().isoformat()
    counts = state.daily.get(today, {})
    events = [entry for entry in state.activity if str(entry.get("at", "")).startswith(today)]
    accepted = sum(1 for entry in events if entry.get("outcome") == "applied")
    missed = sum(1 for entry in events if entry.get("outcome") in ("missed", "gone"))
    lines.append(
        f'<p class="muted">Today: {counts.get("reads", 0)} calendar reads, {counts.get("failed", 0)} failed; '
        f"{accepted} accepted, {missed} missed.</p>"
    )
    return '<div class="card"><strong>Right now</strong>' + "".join(lines) + "</div>"


def _recent_activity(settings: Settings, state: BotState) -> str:
    items = []
    for entry in reversed(state.activity[-ACTIVITY_SHOWN:]):
        outcome = OUTCOMES.get(str(entry.get("outcome")), html.escape(str(entry.get("outcome"))))
        try:
            slot = f"{Slot.parse(entry['slot']).label()}: " if entry.get("slot") else ""
        except (TypeError, ValueError):
            slot = ""
        seconds = entry.get("seconds")
        timing = f", {seconds} s after the bot noticed it" if isinstance(seconds, (int, float)) else ""
        items.append(f"<li>{_when(entry.get('at'), settings.timezone)}: {slot}{outcome}{timing}</li>")
    body = "".join(items) or "<li>No openings yet.</li>"
    return f'<div class="card"><strong>Recent activity</strong><ul>{body}</ul></div>'


def _status_page(
    settings: Settings,
    control: ControlState,
    state: BotState,
    csrf: str,
    now: dt.datetime,
    flash: str | None,
    status_data: dict | None = None,
) -> str:
    status_data = status_data or {}
    token = f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
    parts = ["<h1>Elisa bot</h1>"]
    if flash in FLASH:
        parts.append(f'<div class="card flash">{FLASH[flash]}</div>')

    if control.enabled and settings.quick_check_seconds:
        status = (
            f'<p class="state on">ON</p><p>Checking for new openings every {settings.quick_check_seconds} seconds; '
            f'reading the calendar every {control.poll_seconds} seconds.</p>'
        )
        button = '<button class="big turn-off" name="enabled" value="off" type="submit">Turn OFF</button>'
    elif control.enabled:
        status = f'<p class="state on">ON</p><p>Checking every {control.poll_seconds} seconds.</p>'
        button = '<button class="big turn-off" name="enabled" value="off" type="submit">Turn OFF</button>'
    else:
        status = '<p class="state off">OFF</p><p>Not checking.</p>'
        button = '<button class="big turn-on" name="enabled" value="on" type="submit">Turn ON</button>'
    mode = (
        "Test mode: finds openings and emails you, but doesn't click Accept."
        if settings.test_mode
        else "Live: accepts openings automatically."
    )
    if state.last_run_ok is None:
        result = ""
    elif state.last_run_ok:
        result = " (OK)"
    else:
        result = f" (failed: {html.escape(state.last_error or 'unknown')})"
    last = f'<p class="muted">Last check: {_when(state.last_run_at, settings.timezone)}{result}</p>'
    stale = ""
    if control.enabled and state.last_run_at:
        try:
            age = (now - dt.datetime.fromisoformat(state.last_run_at)).total_seconds()
        except ValueError:
            age = 0
        if age > max(3 * control.poll_seconds, 90):
            stale = f'<p class="warn">No check for {int(age // 60)} min. The bot may not be running.</p>'
    parts.append(
        f'<form class="card" method="post" action="/switch">{token}{status}'
        f'<p class="muted">{mode}</p>{last}{stale}{button}</form>'
    )

    parts.append(_right_now(settings, control, state, status_data, now))
    parts.append(_recent_activity(settings, state))

    accepted = sorted((Slot.parse(key) for key in state.applied), reverse=True)[:10]
    items = "".join(f"<li>{slot.label()}</li>" for slot in accepted) or "<li>None yet.</li>"
    parts.append(f'<div class="card"><strong>Accepted recently</strong><ul>{items}</ul></div>')

    days = [dt.date.fromisoformat(day) for day in control.days_off]
    rows = "".join(
        f'<li><span>{day:%a %b} {day.day}, {day.year}</span><form method="post" action="/days-off">{token}'
        f'<input type="hidden" name="remove" value="{day.isoformat()}">'
        '<button class="secondary small" type="submit">Remove</button></form></li>'
        for day in days
    ) or "<li>None.</li>"
    parts.append(
        '<div class="card"><strong>Days off</strong>'
        "<p class=\"muted\">The bot doesn't accept jobs on these days.</p>"
        f'<ul class="plain">{rows}</ul>'
        f'<form class="row" method="post" action="/days-off">{token}'
        f'<input type="date" name="add" min="{now.date().isoformat()}" required aria-label="Day off">'
        '<button class="secondary" type="submit">Add</button></form></div>'
    )

    jobs_options = "".join(
        f'<option value="{n}"{" selected" if n == control.max_jobs_per_day else ""}>{n}</option>'
        for n in range(1, MAX_JOBS_PER_DAY + 1)
    )
    gap = settings.job_gap_minutes / 60
    parts.append(
        f'<form class="card" method="post" action="/jobs-per-day">{token}<strong>Jobs per day</strong>'
        f'<p class="muted">At most this many jobs a day, starting at least {gap:g} hours apart (Eastern time).</p>'
        f'<div class="row"><select id="jobs" name="max_jobs_per_day" aria-label="Jobs per day">{jobs_options}</select>'
        '<button class="secondary" type="submit">Save</button></div></form>'
    )

    choices = sorted({c for c in INTERVAL_CHOICES if settings.min_poll_seconds <= c <= settings.max_poll_seconds}
                     | {control.poll_seconds})
    options = "".join(
        f'<option value="{c}"{" selected" if c == control.poll_seconds else ""}>every {c} seconds</option>' for c in choices
    )
    parts.append(
        f'<form class="card row" method="post" action="/interval">{token}'
        f'<label for="interval">{"Calendar" if settings.quick_check_seconds else "Check"}</label>'
        f'<select id="interval" name="poll_seconds">{options}</select>'
        '<button class="secondary" type="submit">Save</button></form>'
    )

    parts.append(
        f'<div class="card row"><form method="post" action="/test-alert">{token}'
        '<button class="secondary" type="submit">Send test alert</button></form>'
        f'<form method="post" action="/logout">{token}'
        '<button class="secondary" type="submit">Sign out</button></form></div>'
    )
    return "".join(parts)


def create_control_app(
    settings: Settings,
    *,
    notifier_factory: Callable[[Settings], Notifier] = build_notifier,
    now: Callable[[], dt.datetime] | None = None,
) -> FastAPI:
    settings.require_control_secrets()
    key = (settings.control_secret_key or "").encode()
    controls = ControlStore(settings)
    states = StateStore(settings.state_path)
    clock = now or (lambda: now_in(settings.timezone))
    failed_logins: dict[str, deque[float]] = defaultdict(deque)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def mac(payload: str) -> str:
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def session_csrf(request: Request) -> str | None:
        payload, _, signature = (request.cookies.get(COOKIE) or "").rpartition(".")
        if not payload or not hmac.compare_digest(signature, mac(payload)):
            return None
        expires, _, csrf = payload.partition(":")
        if not expires.isdigit() or int(expires) < time.time() or not csrf:
            return None
        return csrf

    async def form(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8", "replace")
        return {name: values[0] for name, values in parse_qs(body).items()}

    async def checked_form(request: Request) -> tuple[dict[str, str] | None, Response | None]:
        csrf = session_csrf(request)
        if csrf is None:
            return None, RedirectResponse("/login", status_code=303)
        data = await form(request)
        if not hmac.compare_digest(data.get("csrf", ""), csrf):
            return None, PlainTextResponse("Forbidden", status_code=403)
        return data, None

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse(MANIFEST, media_type="application/manifest+json")

    @app.get("/icon-{size}.png")
    async def icon(size: int):
        if size not in ICON_SIZES:
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(STATIC / f"icon-{size}.png", media_type="image/png")

    @app.get("/login")
    async def login_page():
        return _page(_login_page())

    @app.post("/login")
    async def login(request: Request):
        client = request.client.host if request.client else "unknown"
        attempts = failed_logins[client]
        while attempts and attempts[0] < time.time() - LOGIN_WINDOW_S:
            attempts.popleft()
        if len(attempts) >= LOGIN_LIMIT:
            return _page(_login_page("Too many attempts. Try again in 15 minutes."), status=429)
        password = (await form(request)).get("password", "")
        if not hmac.compare_digest(password.encode(), (settings.control_password or "").encode()):
            attempts.append(time.time())
            return _page(_login_page("Wrong password."), status=401)
        attempts.clear()
        payload = f"{int(time.time()) + SESSION_SECONDS}:{secrets.token_urlsafe(16)}"
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            f"{payload}.{mac(payload)}",
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.control_cookie_secure,
            samesite="strict",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        _, problem = await checked_form(request)
        if problem is not None:
            return problem
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    async def home(request: Request, msg: str | None = None):
        csrf = session_csrf(request)
        if csrf is None:
            return RedirectResponse("/login", status_code=303)
        body = _status_page(settings, controls.load(), states.load(), csrf, clock(), msg, _read_status(settings))
        return _page(body, refresh=60)

    @app.post("/switch")
    async def switch(request: Request):
        data, problem = await checked_form(request)
        if problem is not None:
            return problem
        if data.get("enabled") in ("on", "off"):
            controls.update(by="web", now=clock(), enabled=data["enabled"] == "on")
        return RedirectResponse("/", status_code=303)

    @app.post("/interval")
    async def interval(request: Request):
        data, problem = await checked_form(request)
        if problem is not None:
            return problem
        try:
            seconds = int(data.get("poll_seconds", ""))
        except ValueError:
            return RedirectResponse("/", status_code=303)
        controls.update(by="web", now=clock(), poll_seconds=seconds)
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/jobs-per-day")
    async def jobs_per_day(request: Request):
        data, problem = await checked_form(request)
        if problem is not None:
            return problem
        try:
            count = int(data.get("max_jobs_per_day", ""))
        except ValueError:
            return RedirectResponse("/", status_code=303)
        controls.update(by="web", now=clock(), max_jobs_per_day=count)
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/days-off")
    async def days_off(request: Request):
        data, problem = await checked_form(request)
        if problem is not None:
            return problem

        def day(name: str) -> dt.date | None:
            try:
                return dt.date.fromisoformat(data.get(name, ""))
            except ValueError:
                return None

        add, remove = day("add"), day("remove")
        if add is None and remove is None:
            return RedirectResponse("/", status_code=303)
        controls.update(by="web", now=clock(), add_day_off=add, remove_day_off=remove)
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/test-alert")
    async def test_alert(request: Request):
        _, problem = await checked_form(request)
        if problem is not None:
            return problem
        sent = await notifier_factory(settings).send(test_message())
        return RedirectResponse("/?msg=sent" if sent else "/?msg=failed", status_code=303)

    return app
