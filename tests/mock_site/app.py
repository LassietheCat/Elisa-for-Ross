from __future__ import annotations

import asyncio
import json
import secrets
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .pages import DASHBOARD_HTML, LOGIN_HTML

MOCK_EMAIL = "interpreter@example.test"
MOCK_PASSWORD = "mock-password-123"
SESSION_COOKIE = "mock_session"
SHOWN_STATUSES = ("available", "applied", "assigned", "confirmed")
HOLIDAYS = {
    "2026-09-07": "Labor Day",
    "2026-10-12": "Columbus Day",
    "2026-11-11": "Veterans Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
}

DEFAULT_CONFIG = {
    "today": "2026-10-07",
    "loading_delay_ms": 150,
    "login_delay_ms": 100,
    "invalid_credentials": False,
    "require_verification": False,
    "blank_calendar_times": 0,
    "apply_mode": "success",
    "accept_result_status": "applied",
    "confirm_dialog": False,
    "button_label": "Accept",
}


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.config = dict(DEFAULT_CONFIG)
            self.work_orders: list[dict] = []
            self.sessions: set[str] = set()
            self.stats = {"login_attempts": 0, "logins_ok": 0, "calendar_fetches": 0, "details_opened": 0, "accept_calls": []}

    def set_work_orders(self, items: list[dict]) -> None:
        with self.lock:
            self.work_orders = [
                {"id": str(index), "date": item["date"], "half": item["half"], "status": item["status"]}
                for index, item in enumerate(items, start=1)
            ]


def create_app(state: MockState) -> FastAPI:
    app = FastAPI()

    def signed_in(request: Request) -> bool:
        return request.cookies.get(SESSION_COOKIE) in state.sessions

    def unauthorized() -> JSONResponse:
        return JSONResponse({"message": "Unauthorized"}, status_code=401)

    @app.get("/")
    async def root():
        return RedirectResponse("/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return LOGIN_HTML.replace("__LOGIN_DELAY_MS__", str(int(state.config["login_delay_ms"])))

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        with state.lock:
            state.stats["login_attempts"] += 1
            config = dict(state.config)
        if config["require_verification"]:
            return JSONResponse({"ok": False, "message": "Enter the verification code we sent to your email."})
        if config["invalid_credentials"] or body.get("email") != MOCK_EMAIL or body.get("password") != MOCK_PASSWORD:
            return JSONResponse({"ok": False, "message": "Invalid email or password."}, status_code=401)
        token = secrets.token_hex(16)
        with state.lock:
            state.sessions.add(token)
            state.stats["logins_ok"] += 1
        response = JSONResponse({"ok": True})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
        return response

    @app.get("/my/dashboard/main-dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not signed_in(request):
            return RedirectResponse("/login", status_code=302)
        return DASHBOARD_HTML.replace("__CONFIG__", json.dumps({"today": state.config["today"]}))

    @app.get("/api/mock/calendar")
    async def calendar(request: Request, year: int, month: int):
        if not signed_in(request):
            return unauthorized()
        await asyncio.sleep(state.config["loading_delay_ms"] / 1000)
        prefix = f"{year:04d}-{month:02d}-"
        with state.lock:
            state.stats["calendar_fetches"] += 1
            blank = state.config["blank_calendar_times"] > 0
            if blank:
                state.config["blank_calendar_times"] -= 1
            cells: dict = {}
            for wo in state.work_orders:
                if wo["status"] in SHOWN_STATUSES and wo["date"].startswith(prefix):
                    half = cells.setdefault(wo["date"], {}).setdefault(wo["half"], {})
                    half[wo["status"]] = half.get(wo["status"], 0) + 1
            counts = {s: sum(1 for wo in state.work_orders if wo["status"] == s) for s in ("applied", "assigned", "confirmed")}
            today = state.config["today"]
        holidays = {day: name for day, name in HOLIDAYS.items() if day.startswith(prefix)}
        return {"year": year, "month": month, "today": today, "blank": blank,
                "cells": cells, "holidays": holidays, "counts": counts}

    @app.get("/api/mock/details")
    async def details(request: Request, slot: str):
        if not signed_in(request):
            return unauthorized()
        day, half = slot.split(" ")
        with state.lock:
            state.stats["details_opened"] += 1
            open_now = [wo for wo in state.work_orders
                        if wo["date"] == day and wo["half"] == half and wo["status"] == "available"]
            label = None if state.config["apply_mode"] == "no_button" else state.config["button_label"]
            confirm = state.config["confirm_dialog"]
        return {
            "work_orders": [
                {"id": wo["id"], "number": f"WO {9990000 + int(wo['id'])}", "court": "CONFIDENTIAL-COURT",
                 "language": "CONFIDENTIAL-LANG", "judge": "CONFIDENTIAL-JUDGE",
                 "time": "09:00 AM" if half == "AM" else "02:00 PM"}
                for wo in open_now
            ],
            "button_label": label,
            "confirm": confirm,
        }

    @app.post("/api/mock/accept/{wo_id}")
    async def accept(request: Request, wo_id: str):
        if not signed_in(request):
            return unauthorized()
        await asyncio.sleep(0.05)
        with state.lock:
            state.stats["accept_calls"].append(wo_id)
            wo = next((w for w in state.work_orders if w["id"] == wo_id), None)
            mode = state.config["apply_mode"]
            if mode == "error":
                return JSONResponse({"message": "Unexpected server error."}, status_code=500)
            if wo is None or wo["status"] != "available" or mode == "taken":
                if wo is not None and mode == "taken":
                    wo["status"] = "taken"
                return JSONResponse({"message": "This work order is no longer available."}, status_code=409)
            wo["status"] = state.config["accept_result_status"]
        return {"message": "Work order accepted successfully."}

    return app
