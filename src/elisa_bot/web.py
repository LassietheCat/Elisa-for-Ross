from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import logging
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
from .models import Half, Slot
from .notify import Notifier, build_notifier, test_message
from .state import MAX_JOBS_PER_DAY, SERVICE_TYPES, BotState, ControlState, ControlStore, StateStore, off_entries
from .timeutil import now_in

log = logging.getLogger(__name__)

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
    "background_color": "#f2f2f7",
    "theme_color": "#34c759",
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
    "too soon": "skipped, starts too soon",
    "refused": "Elisa refused the Accept",
    "gone": "a new opening was gone before the bot could read it",
    "landed": "on the calendar after all",
    "not landed": "accepted, but not on the calendar; check Elisa",
}
FLASH = {
    "sent": "Test alert sent. Check your inbox.",
    "failed": "The test alert could not be sent. Check the email settings.",
    "saved": "Saved.",
    "types": "Pick at least one job type.",
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
<meta name="theme-color" content="#34c759">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Elisa bot">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
%%REFRESH%%<title>Elisa bot</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:light dark;
  --bg:#f2f2f7;--card:#fff;--card2:#f9f9fb;
  --text:#1c1c1e;--muted:#8e8e93;--faint:#c7c7cc;
  --on:#34c759;--on-d:#e6fbed;--on-t:#1a7f37;
  --stop:#ff3b30;--stop-d:#fff0ef;--stop-t:#c62828;
  --warn:#ff9500;--warn-t:#7a4000;
  --accent:#007aff;--accent-d:#e5f0ff;
  --border:#e5e5ea;
  --sh:0 1px 3px rgba(0,0,0,.07),0 4px 12px rgba(0,0,0,.05);
  --r:16px;--r2:12px}
@media(prefers-color-scheme:dark){:root{
  --bg:#1c1c1e;--card:#2c2c2e;--card2:#3a3a3c;
  --text:#f2f2f7;--muted:#8e8e93;--faint:#48484a;
  --on:#30d158;--on-d:#0a2d14;--on-t:#30d158;
  --stop:#ff453a;--stop-d:#2d0d0c;--stop-t:#ff453a;
  --warn:#ff9f0a;--warn-t:#ffc060;
  --accent:#0a84ff;--accent-d:#001a3a;
  --border:#3a3a3c;
  --sh:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.25)}}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.hdr{padding:14px 20px 6px;max-width:980px;margin:0 auto}
.hdr h1{font-size:14px;font-weight:600;margin:0;color:var(--muted);letter-spacing:.02em}
.wrap{max-width:980px;margin:0 auto;padding:8px 16px 48px;
  display:grid;grid-template-columns:1fr;gap:10px}
@media(min-width:640px){.wrap{grid-template-columns:1fr 1fr;
  grid-template-areas:"notif notif" "hero hero" "nums nums" "act cfg" "foot foot"}}
.card{background:var(--card);border-radius:var(--r);padding:18px 20px;box-shadow:var(--sh)}
.clabel{font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.7px;color:var(--muted);margin:0 0 12px}
.notif{grid-area:notif}
.notif-inner{display:flex;align-items:center;gap:10px;padding:11px 16px;
  border-radius:var(--r2);background:var(--on-d);border:1px solid var(--on);
  font-size:14px;font-weight:500;color:var(--on-t)}
.hero{grid-area:hero}
.badge-row{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;
  border-radius:999px;font-size:12px;font-weight:700}
.badge.on{background:var(--on-d);color:var(--on-t)}
.badge.off{background:var(--border);color:var(--muted)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.mode-tag{font-size:12px;color:var(--muted);padding:4px 9px;
  border-radius:999px;background:var(--card2);border:1px solid var(--border)}
.hero-desc{font-size:14px;color:var(--muted);margin:0 0 14px}
.hero-warn{font-size:14px;font-weight:600;color:var(--stop-t);
  background:var(--stop-d);border-radius:var(--r2);padding:9px 13px;margin-bottom:12px}
.tog{width:100%;padding:16px 20px;border:0;border-radius:var(--r2);
  font:700 17px/1 inherit;color:#fff;cursor:pointer;
  display:flex;align-items:center;justify-content:center;gap:10px;
  transition:filter .15s}
.tog:active{filter:brightness(.87)}
.tog.start{background:linear-gradient(135deg,#2ecc71,#1e9e56)}
.tog.stop{background:linear-gradient(135deg,#ff6b6b,#c0392b)}
.irows{margin-top:14px;border-top:1px solid var(--border);padding-top:12px;
  display:flex;flex-direction:column;gap:6px}
.ir{display:flex;align-items:baseline;gap:6px;font-size:13px;color:var(--muted)}
.ir b{color:var(--text);font-weight:500}
.nums{grid-area:nums;display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.num{background:var(--card);border-radius:var(--r2);padding:12px 14px;
  box-shadow:var(--sh);text-align:center}
.num .n{font-size:28px;font-weight:700;line-height:1}
.num .l{font-size:12px;color:var(--muted);font-weight:500;margin-top:3px}
.c-green .n{color:var(--on-t)} .c-red .n{color:var(--stop-t)} .c-amber .n{color:var(--warn)}
.act{grid-area:act}
.tl{list-style:none;padding:0;margin:0}
.tli{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)}
.tli:last-child{border-bottom:0}
.tl-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;margin-top:4px}
.d-ok{background:var(--on)} .d-skip{background:var(--faint)}
.d-warn{background:var(--warn)} .d-err{background:var(--stop)}
.tl-slot{font-size:14px;font-weight:600;display:block;margin-bottom:1px}
.tl-out{font-size:13px;color:var(--muted)}
.tl-time{font-size:12px;color:var(--faint);margin-top:2px}
.cfg{grid-area:cfg;display:flex;flex-direction:column;gap:8px}
details{background:var(--card);border-radius:var(--r2);box-shadow:var(--sh);overflow:hidden}
details[open]{box-shadow:var(--sh),0 0 0 1.5px var(--border)}
summary{padding:13px 17px;font-size:14px;font-weight:600;cursor:pointer;
  list-style:none;display:flex;align-items:center;justify-content:space-between;
  user-select:none;-webkit-user-select:none}
summary::-webkit-details-marker{display:none}
.chev{color:var(--muted);transition:transform .2s;display:inline-block;line-height:1}
details[open] .chev{transform:rotate(180deg)}
.dbody{padding:0 17px 15px;border-top:1px solid var(--border)}
.ddesc{font-size:13px;color:var(--muted);margin:9px 0 10px}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.pills input[type=checkbox]{position:absolute;opacity:0;width:1px;height:1px;
  overflow:hidden;clip:rect(0,0,0,0)}
.pills label{display:inline-flex;align-items:center;padding:7px 15px;
  border-radius:999px;border:1.5px solid var(--border);font-size:14px;font-weight:500;
  cursor:pointer;transition:all .15s;user-select:none;background:var(--card)}
.pills input[type=checkbox]:checked+label{background:var(--on-d);border-color:var(--on);
  color:var(--on-t);font-weight:600}
.seg{display:inline-flex;background:var(--card2);border-radius:10px;
  padding:3px;gap:2px;border:1px solid var(--border)}
.seg input[type=radio]{position:absolute;opacity:0;width:1px;height:1px;
  overflow:hidden;clip:rect(0,0,0,0)}
.seg label{padding:5px 11px;border-radius:8px;font-size:13px;font-weight:500;
  cursor:pointer;color:var(--muted);transition:all .15s;
  user-select:none;-webkit-user-select:none;white-space:nowrap}
.seg input[type=radio]:checked+label{background:var(--card);color:var(--text);
  font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.numpick{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.numpick input[type=radio]{position:absolute;opacity:0;width:1px;height:1px;
  overflow:hidden;clip:rect(0,0,0,0)}
.numpick label{width:38px;height:38px;display:flex;align-items:center;
  justify-content:center;border-radius:50%;border:1.5px solid var(--border);
  font-size:14px;font-weight:600;cursor:pointer;transition:all .15s;
  user-select:none;background:var(--card);color:var(--text)}
.numpick input[type=radio]:checked+label{background:var(--accent);
  border-color:var(--accent);color:#fff}
.selw{position:relative;display:inline-block}
.selw::after{content:"▾";position:absolute;right:10px;top:50%;
  transform:translateY(-50%);pointer-events:none;font-size:11px;color:var(--muted)}
select{font:inherit;font-size:14px;padding:8px 28px 8px 11px;border-radius:10px;
  border:1px solid var(--border);background:var(--card);color:var(--text);
  appearance:none;-webkit-appearance:none;cursor:pointer}
input[type=date]{font:inherit;font-size:14px;padding:8px 10px;border-radius:10px;
  border:1px solid var(--border);background:var(--card);color:var(--text);
  min-width:0;flex:1}
input[type=password]{font:inherit;font-size:15px;padding:11px 14px;width:100%;
  border-radius:var(--r2);border:1.5px solid var(--border);
  background:var(--card2);color:var(--text);margin-bottom:14px}
input[type=password]:focus{outline:none;border-color:var(--accent)}
.dlist{list-style:none;padding:0;margin:0 0 12px}
.dlist li{display:flex;align-items:center;justify-content:space-between;
  gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:14px}
.dlist li:last-child{border-bottom:0}
.frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.btn{font:500 14px/1 inherit;border:1.5px solid var(--border);border-radius:10px;
  padding:8px 15px;background:var(--card);color:var(--text);cursor:pointer;
  white-space:nowrap;transition:opacity .15s}
.btn:active{opacity:.7}
.btn-sm{font-size:13px;padding:5px 10px;border-radius:8px}
.b-primary{border-color:var(--accent);color:var(--accent)}
.b-save{border-color:var(--on);color:var(--on-t)}
.b-danger{border-color:var(--stop-d);color:var(--stop-t)}
.foot{grid-area:foot;display:flex;gap:8px}
.foot form{flex:1}
.foot button{width:100%}
</style></head>
<body>
<div class="hdr"><h1>Elisa bot</h1></div>
<div class="wrap">%%BODY%%</div>
</body></html>"""


def _page(body: str, *, status: int = 200, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">\n' if refresh else ""
    return HTMLResponse(LAYOUT.replace("%%REFRESH%%", meta).replace("%%BODY%%", body), status_code=status)


def _login_page(message: str = "") -> str:
    err = (
        f'<div style="font-size:14px;font-weight:600;color:var(--stop-t);background:var(--stop-d);'
        f'border-radius:10px;padding:9px 13px;margin-bottom:12px">{html.escape(message)}</div>'
    ) if message else ""
    return (
        '<div style="grid-column:1/-1;max-width:400px;margin:40px auto 0;width:100%;padding:0 4px">'
        '<h1 style="font-size:22px;font-weight:700;text-align:center;margin:0 0 20px">Elisa bot</h1>'
        '<div class="card"><form method="post" action="/login">'
        '<label for="pw" style="display:block;font-size:13px;font-weight:600;'
        'color:var(--muted);margin-bottom:6px">Password</label>'
        '<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>'
        f"{err}"
        '<button class="tog start" type="submit">Sign in</button>'
        "</form></div></div>"
    )


def _when(text: str | None, tz: str) -> str:
    if not text:
        return "never"
    try:
        moment = dt.datetime.fromisoformat(text).astimezone(ZoneInfo(tz))
    except ValueError:
        return "unknown"
    return f"{moment:%a %b} {moment.day}, {moment.hour % 12 or 12}:{moment:%M:%S} {moment:%p}"


def _day_off_label(entry: str) -> str:
    text, _, half = entry.partition(" ")
    day = dt.date.fromisoformat(text)
    return f"{day:%a %b} {day.day}, {day.year}" + (f", {half} only" if half else "")


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


_DOT = {
    "applied": "d-ok", "missed": "d-warn", "gone": "d-warn",
    "error": "d-err", "unverified": "d-warn", "refused": "d-err",
    "test_mode": "d-skip", "skipped": "d-skip", "day off": "d-skip",
    "too close": "d-skip", "day full": "d-skip", "too soon": "d-skip",
    "landed": "d-ok", "not landed": "d-err",
}


def _recent_activity(settings: Settings, state: BotState) -> str:
    items = []
    for entry in reversed(state.activity[-ACTIVITY_SHOWN:]):
        key = str(entry.get("outcome"))
        outcome = OUTCOMES.get(key, html.escape(key))
        dot = _DOT.get(key, "d-skip")
        try:
            slot = Slot.parse(entry["slot"]).label() if entry.get("slot") else ""
        except (TypeError, ValueError):
            slot = ""
        seconds = entry.get("seconds")
        timing = f", {seconds} s after the bot noticed it" if isinstance(seconds, (int, float)) else ""
        prefix = f"{html.escape(slot)}: " if slot else ""
        when = _when(entry.get("at"), settings.timezone)
        items.append(
            f'<li class="tli"><div class="tl-dot {dot}"></div>'
            f'<div><div class="tl-slot">{prefix}{outcome}{timing}</div>'
            f'<div class="tl-time">{when}</div></div></li>'
        )
    body = "".join(items) or '<li class="tli"><div class="tl-slot" style="color:var(--muted)">No openings yet.</div></li>'
    return f'<div class="card act"><p class="clabel">Recent activity</p><ul class="tl">{body}</ul></div>'


def _hero_card(
    settings: Settings, control: ControlState, state: BotState,
    status: dict, now: dt.datetime, token: str,
) -> str:
    tz = settings.timezone
    enabled = control.enabled

    badge = (
        f'<span class="badge {"on" if enabled else "off"}">'
        f'<span class="dot"></span>{"ON" if enabled else "OFF"}</span>'
    )
    mode_tag = f'<span class="mode-tag">{"Test mode" if settings.test_mode else "Live"}</span>'

    if enabled and settings.quick_check_seconds:
        desc = f"Checking every {settings.quick_check_seconds} s · calendar every {control.poll_seconds} s"
    elif enabled:
        desc = f"Checking every {control.poll_seconds} s"
    else:
        desc = "Not checking"

    stale = ""
    if enabled and state.last_run_at:
        try:
            age = (now - dt.datetime.fromisoformat(state.last_run_at)).total_seconds()
        except ValueError:
            age = 0
        if age > max(3 * control.poll_seconds, 90):
            stale = f'<div class="hero-warn">&#9888; No check for {int(age // 60)} min — bot may not be running.</div>'

    btn_cls = "stop" if enabled else "start"
    btn_label = "&#9646; Turn OFF" if enabled else "&#9654; Turn ON"
    btn_val = "off" if enabled else "on"

    info = []
    quick = status.get("quick") if isinstance(status.get("quick"), dict) else {}
    if enabled:
        if settings.quick_check_seconds:
            if quick.get("paused"):
                info.append('<div class="ir">Quick checks paused (rate limit) — resumes after next read</div>')
            elif quick.get("answered_at"):
                count = quick.get("open_count")
                listed = f" · <b>{count} open</b>" if isinstance(count, int) else ""
                info.append(f'<div class="ir">Last quick check <b>{_when(quick["answered_at"], tz)}</b>{listed}</div>')
        if status.get("signed_in_at"):
            info.append(f'<div class="ir">Signed in since <b>{_when(status["signed_in_at"], tz)}</b></div>')
        upd_age = _age(status.get("updated_at"), now)
        if upd_age is not None and upd_age > STATUS_STALE_S:
            info.append(f'<div class="ir" style="color:var(--stop-t)">No status update for {int(upd_age // 60)} min</div>')

    if state.last_run_ok is None:
        result = ""
    elif state.last_run_ok:
        result = " (OK)"
    else:
        result = f" (failed: {html.escape(state.last_error or 'unknown')})"
    if state.last_run_at:
        info.append(f'<div class="ir">Last check: {_when(state.last_run_at, tz)}{result}</div>')

    beat = status.get("heartbeat") if isinstance(status.get("heartbeat"), dict) else {}
    if not beat.get("configured"):
        info.append('<div class="ir">Outside alarm: not configured</div>')
    elif beat.get("failing"):
        info.append('<div class="ir" style="color:var(--stop-t)">Outside alarm: last signal failed</div>')
    elif beat.get("ok_at"):
        info.append(f'<div class="ir">Outside alarm: OK · <b>{_when(beat["ok_at"], tz)}</b></div>')

    phones = len(settings.alert_sms_to)
    if settings.sms_enabled:
        info.append(f'<div class="ir">Text alerts: on · <b>{phones} phone{"" if phones == 1 else "s"}</b></div>')
    else:
        info.append('<div class="ir">Text alerts: not configured</div>')

    irows = f'<div class="irows">{"".join(info)}</div>' if info else ""

    return (
        f'<div class="card hero">'
        f'<div class="badge-row">{badge}{mode_tag}</div>'
        f'<p class="hero-desc">{html.escape(desc)}</p>'
        f'{stale}'
        f'<form method="post" action="/switch">{token}'
        f'<button class="tog {btn_cls}" name="enabled" value="{btn_val}" type="submit">{btn_label}</button>'
        f'</form>{irows}</div>'
    )


def _stats_row(settings: Settings, state: BotState, now: dt.datetime) -> str:
    today = now.date().isoformat()
    counts = state.daily.get(today, {})
    events = [e for e in state.activity if str(e.get("at", "")).startswith(today)]
    accepted = sum(1 for e in events if e.get("outcome") in ("applied", "landed"))
    missed = sum(1 for e in events if e.get("outcome") in ("missed", "gone"))
    reads = counts.get("reads", 0)
    failed = counts.get("failed", 0)

    def _stat(n: int, label: str, color: str = "") -> str:
        cls = f' {color}' if color else ''
        return f'<div class="num{cls}"><div class="n">{n}</div><div class="l">{label}</div></div>'

    return (
        f'<div class="nums">'
        f'{_stat(reads, "reads today", "c-red" if failed > 0 else "")}'
        f'{_stat(accepted, "accepted today", "c-green" if accepted > 0 else "")}'
        f'{_stat(missed, "missed today", "c-amber" if missed > 0 else "")}'
        f'</div>'
    )


def _settings_panel(settings: Settings, control: ControlState, now: dt.datetime, token: str) -> str:
    sections = []

    rows = "".join(
        f'<li><span>{_day_off_label(e)}</span>'
        f'<form method="post" action="/days-off" style="display:inline-flex">{token}'
        f'<input type="hidden" name="remove" value="{html.escape(e)}">'
        f'<button class="btn btn-sm" type="submit">Remove</button></form></li>'
        for e in control.days_off
    ) or '<li style="color:var(--muted);font-size:14px">None set.</li>'
    sections.append(
        '<details open><summary><span>Days off</span><span class="chev">&#9660;</span></summary>'
        '<div class="dbody">'
        '<p class="ddesc">The bot skips jobs on these days. AM = before noon, PM = noon or later (Eastern).</p>'
        f'<ul class="dlist">{rows}</ul>'
        f'<form method="post" action="/days-off">{token}'
        '<div class="frow">'
        f'<input type="date" name="add" min="{now.date().isoformat()}" required aria-label="Day off">'
        '<div class="seg">'
        '<input type="radio" id="pt-day" name="part" value="day" checked>'
        '<label for="pt-day">All day</label>'
        '<input type="radio" id="pt-am" name="part" value="AM">'
        '<label for="pt-am">AM</label>'
        '<input type="radio" id="pt-pm" name="part" value="PM">'
        '<label for="pt-pm">PM</label>'
        '</div>'
        '<button class="btn b-save" type="submit">Add</button>'
        '</div></form></div></details>'
    )

    gap = settings.job_gap_minutes / 60
    num_opts = "".join(
        f'<input type="radio" id="j{n}" name="max_jobs_per_day" value="{n}"'
        f'{" checked" if n == control.max_jobs_per_day else ""}>'
        f'<label for="j{n}">{n}</label>'
        for n in range(1, MAX_JOBS_PER_DAY + 1)
    )
    sections.append(
        '<details><summary><span>Jobs per day</span><span class="chev">&#9660;</span></summary>'
        '<div class="dbody">'
        f'<p class="ddesc">Max jobs per day, spaced at least {gap:g} hours apart (Eastern).</p>'
        f'<form method="post" action="/jobs-per-day">{token}'
        f'<div class="numpick">{num_opts}</div>'
        '<div class="frow"><button class="btn b-save" type="submit">Save</button></div>'
        '</form></div></details>'
    )

    type_opts = "".join(
        f'<input type="checkbox" id="tp-{code.lower()}" name="types" value="{code}"'
        f'{" checked" if code in control.service_types else ""}>'
        f'<label for="tp-{code.lower()}">{code}</label>'
        for code in SERVICE_TYPES
    )
    sections.append(
        '<details><summary><span>Job types</span><span class="chev">&#9660;</span></summary>'
        '<div class="dbody">'
        '<p class="ddesc">VRI = video remote, OPI = phone, OSI = on-site.</p>'
        f'<form method="post" action="/job-types">{token}'
        f'<div class="pills">{type_opts}</div>'
        '<div class="frow"><button class="btn b-save" type="submit">Save</button></div>'
        '</form></div></details>'
    )

    choices = sorted(
        {c for c in INTERVAL_CHOICES if settings.min_poll_seconds <= c <= settings.max_poll_seconds}
        | {control.poll_seconds}
    )
    opts = "".join(
        f'<option value="{c}"{" selected" if c == control.poll_seconds else ""}>every {c} s</option>'
        for c in choices
    )
    lbl = "Calendar read" if settings.quick_check_seconds else "Check"
    sections.append(
        f'<details><summary><span>{lbl} interval</span><span class="chev">&#9660;</span></summary>'
        '<div class="dbody">'
        f'<form method="post" action="/interval">{token}'
        '<div class="frow">'
        f'<div class="selw"><select name="poll_seconds">{opts}</select></div>'
        '<button class="btn b-save" type="submit">Save</button>'
        '</div></form></div></details>'
    )

    return f'<div class="cfg">{"".join(sections)}</div>'


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
    parts = []

    if flash in FLASH:
        parts.append(f'<div class="notif"><div class="notif-inner">{html.escape(FLASH[flash])}</div></div>')

    parts.append(_hero_card(settings, control, state, status_data, now, token))
    parts.append(_stats_row(settings, state, now))
    parts.append(_recent_activity(settings, state))
    parts.append(_settings_panel(settings, control, now, token))
    parts.append(
        f'<div class="foot">'
        f'<form method="post" action="/test-alert">{token}'
        f'<button class="btn b-primary" type="submit">Send test alert</button></form>'
        f'<form method="post" action="/logout">{token}'
        f'<button class="btn b-danger" type="submit">Sign out</button></form>'
        f'</div>'
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
            log.info("Page: switched %s", data["enabled"])
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
        control = controls.update(by="web", now=clock(), poll_seconds=seconds)
        log.info("Page: calendar interval set to %d s", control.poll_seconds)
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
        control = controls.update(by="web", now=clock(), max_jobs_per_day=count)
        log.info("Page: jobs per day set to %d", control.max_jobs_per_day)
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/job-types")
    async def job_types(request: Request):
        _, problem = await checked_form(request)
        if problem is not None:
            return problem
        posted = parse_qs((await request.body()).decode("utf-8", "replace")).get("types", [])
        chosen = [code for code in SERVICE_TYPES if code in {value.strip().upper() for value in posted}]
        if not chosen:
            return RedirectResponse("/?msg=types", status_code=303)
        control = controls.update(by="web", now=clock(), service_types=chosen)
        log.info("Page: job types set to %s", ", ".join(control.service_types))
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/days-off")
    async def days_off(request: Request):
        data, problem = await checked_form(request)
        if problem is not None:
            return problem

        def day(text: str) -> dt.date | None:
            try:
                return dt.date.fromisoformat(text)
            except ValueError:
                return None

        add = day(data.get("add", ""))
        part = data.get("part", "").strip().upper()
        if add is not None and part in (Half.AM.value, Half.PM.value):
            add = Slot(add, Half(part))
        try:
            remove = Slot.parse(data.get("remove", ""))
        except ValueError:
            remove = day(data.get("remove", ""))
        if add is None and remove is None:
            return RedirectResponse("/", status_code=303)
        before = controls.load().days_off_slots()
        after = controls.update(by="web", now=clock(), add_day_off=add, remove_day_off=remove).days_off_slots()
        for action, changed in (("added", after - before), ("removed", before - after)):
            for entry in off_entries(changed):
                log.info("Page: day off %s: %s", action, entry)
        return RedirectResponse("/?msg=saved", status_code=303)

    @app.post("/test-alert")
    async def test_alert(request: Request):
        _, problem = await checked_form(request)
        if problem is not None:
            return problem
        sent = await notifier_factory(settings).send(test_message())
        return RedirectResponse("/?msg=sent" if sent else "/?msg=failed", status_code=303)

    return app
