from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from elisa_bot.config import Settings
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SiteSession
from tests.mock_site.server import MOCK_EMAIL, MOCK_PASSWORD, MockServer

FIXED_NOW = dt.datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))


def fixed_now() -> dt.datetime:
    return FIXED_NOW


def wo(date: str, half: str, status: str) -> dict:
    return {"date": date, "half": half, "status": status}


def make_settings(data_dir: Path, base_url: str, **overrides) -> Settings:
    env = {
        "ELISA_BASE_URL": base_url,
        "ELISA_EMAIL": MOCK_EMAIL,
        "ELISA_PASSWORD": MOCK_PASSWORD,
        "DATA_DIR": str(data_dir),
        "TIMEZONE": "America/New_York",
        "HEADLESS": "true",
        "TEST_MODE": "true",
    }
    env.update({key.upper(): str(value) for key, value in overrides.items()})
    return Settings.from_env(env)


@pytest.fixture(scope="session")
def mock_server():
    with MockServer() as server:
        yield server


@pytest.fixture
def mock(mock_server):
    mock_server.reset()
    return mock_server


@pytest.fixture
def settings(tmp_path, mock) -> Settings:
    return make_settings(tmp_path, mock.base_url)


@pytest.fixture
async def session(settings):
    browser_session = SiteSession(settings, Selectors())
    await browser_session.start()
    yield browser_session
    await browser_session.close()
