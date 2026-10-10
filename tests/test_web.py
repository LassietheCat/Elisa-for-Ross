from __future__ import annotations

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from elisa_bot.config import ConfigError, Settings
from elisa_bot.notify import Message
from elisa_bot.state import BotState, ControlStore, StateStore
from elisa_bot.web import COOKIE, create_control_app

NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("America/New_York"))
PASSWORD = "correct horse battery staple"
SECRET = "k" * 40


class Capture:
    name = "capture"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[Message] = []

    async def send(self, message: Message) -> bool:
        self.messages.append(message)
        return self.ok


def make(tmp_path, *, capture: Capture | None = None, **overrides):
    settings = Settings(
        data_dir=tmp_path,
        control_password=PASSWORD,
        control_secret_key=SECRET,
        control_cookie_secure=False,
        **overrides,
    )
    capture = capture or Capture()
    app = create_control_app(settings, notifier_factory=lambda _: capture, now=lambda: NOW)
    return settings, TestClient(app), capture


def sign_in(client: TestClient) -> None:
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


def csrf_of(client: TestClient) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def test_needs_its_secrets(tmp_path):
    with pytest.raises(ConfigError, match="CONTROL_PASSWORD"):
        create_control_app(Settings(data_dir=tmp_path))
    with pytest.raises(ConfigError, match="at least 32"):
        create_control_app(Settings(data_dir=tmp_path, control_password="x", control_secret_key="short"))


def test_pages_require_sign_in(tmp_path):
    _, client, _ = make(tmp_path)
    assert client.get("/healthz").text == "ok"
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.post("/switch", data={"enabled": "on"}, follow_redirects=False).status_code == 303


def test_wrong_password_is_throttled(tmp_path):
    _, client, _ = make(tmp_path)
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429


def test_signed_in_page_starts_off_and_is_locked_down(tmp_path):
    settings, client, _ = make(tmp_path)
    sign_in(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "OFF" in response.text and "Turn ON" in response.text
    assert "Test mode" in response.text
    assert PASSWORD not in response.text and SECRET not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_switch_turns_the_bot_on_and_off(tmp_path):
    settings, client, _ = make(tmp_path)
    sign_in(client)
    client.post("/switch", data={"enabled": "on", "csrf": csrf_of(client)})
    control = ControlStore(settings).load()
    assert control.enabled is True and control.updated_by == "web"
    page = client.get("/").text
    assert "ON" in page and "Checking every 15 seconds." in page and "Turn OFF" in page
    client.post("/switch", data={"enabled": "off", "csrf": csrf_of(client)})
    assert ControlStore(settings).load().enabled is False


def test_forms_need_the_csrf_token(tmp_path):
    settings, client, _ = make(tmp_path)
    sign_in(client)
    assert client.post("/switch", data={"enabled": "on"}).status_code == 403
    assert client.post("/switch", data={"enabled": "on", "csrf": "forged"}).status_code == 403
    assert ControlStore(settings).load().enabled is False


def test_interval_stays_within_limits(tmp_path):
    settings, client, _ = make(tmp_path)
    sign_in(client)
    client.post("/interval", data={"poll_seconds": "5", "csrf": csrf_of(client)})
    assert ControlStore(settings).load().poll_seconds == 15
    client.post("/interval", data={"poll_seconds": "60", "csrf": csrf_of(client)})
    assert ControlStore(settings).load().poll_seconds == 60
    client.post("/interval", data={"poll_seconds": "soon", "csrf": csrf_of(client)})
    assert ControlStore(settings).load().poll_seconds == 60


def test_tampered_cookie_is_rejected(tmp_path):
    _, client, _ = make(tmp_path)
    sign_in(client)
    good = client.cookies.get(COOKIE)
    payload, _, signature = good.rpartition(".")
    later = "9999999999:" + payload.split(":", 1)[1]
    fresh = TestClient(client.app)

    def status(value: str) -> int:
        return fresh.get("/", headers={"cookie": f"{COOKIE}={value}"}, follow_redirects=False).status_code

    assert status(good) == 200
    assert status(f"{payload}.{'0' * 64}") == 303
    assert status(f"{later}.{signature}") == 303
    assert status("garbage") == 303


def test_status_shows_last_check_accepted_slots_and_stale_warning(tmp_path):
    settings, client, _ = make(tmp_path)
    StateStore(settings.state_path).save(
        BotState(
            baseline_done=True,
            last_run_at="2026-10-09T09:50:00-04:00",
            last_run_ok=True,
            applied={"2026-10-14 AM": "2026-10-08T11:00:00-04:00"},
        )
    )
    ControlStore(settings).update(by="test", now=NOW, enabled=True)
    sign_in(client)
    page = client.get("/").text
    assert "Last check: Fri Oct 9, 9:50:00 AM (OK)" in page
    assert "No check for 10 min" in page
    assert "<li>Wed Oct 14 AM</li>" in page


def test_test_alert_button(tmp_path):
    _, client, capture = make(tmp_path)
    sign_in(client)
    response = client.post("/test-alert", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert response.headers["location"] == "/?msg=sent"
    assert capture.messages[0].subject == "Elisa: test alert"
    capture.ok = False
    response = client.post("/test-alert", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert response.headers["location"] == "/?msg=failed"


def test_sign_out(tmp_path):
    _, client, _ = make(tmp_path)
    sign_in(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_control_file_is_owner_only_json(tmp_path):
    settings, client, _ = make(tmp_path)
    sign_in(client)
    client.post("/switch", data={"enabled": "on", "csrf": csrf_of(client)})
    data = json.loads(settings.control_path.read_text())
    assert data["enabled"] is True and data["poll_seconds"] == 15
    assert settings.control_path.stat().st_mode & 0o777 == 0o600
