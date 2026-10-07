from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from elisa_bot.config import ConfigError, Settings, read_env_file
from elisa_bot.logging_setup import RedactingFilter
from elisa_bot.models import Half, Slot
from elisa_bot.site.selectors import Selectors, load_selectors


def test_defaults_are_safe():
    settings = Settings.from_env({})
    assert settings.test_mode is True
    assert settings.apply_on_first_run is False
    assert settings.base_url == "https://www.sosi1.com"


def test_secrets_can_come_from_files_and_never_show_in_repr(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("file-password\n")
    settings = Settings.from_env({"ELISA_EMAIL": "owner@example.test", "ELISA_PASSWORD_FILE": str(secret)})
    assert settings.elisa_password == "file-password"
    assert "file-password" not in repr(settings)
    assert "owner@example.test" not in repr(settings)


def test_bad_values_name_the_setting_not_the_value():
    with pytest.raises(ConfigError, match="TEST_MODE") as info:
        Settings.from_env({"TEST_MODE": "perhaps"})
    assert "perhaps" not in str(info.value)


def test_missing_credentials_are_reported():
    with pytest.raises(ConfigError, match="ELISA_EMAIL, ELISA_PASSWORD"):
        Settings.from_env({}).require_site_credentials()


def test_env_file_is_read_and_real_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTEST_MODE=false\nALERT_EMAIL_TO='a@example.test, b@example.test'\n")
    assert read_env_file(env_file)["TEST_MODE"] == "false"
    settings = Settings.from_env({"TEST_MODE": "true"}, env_file=env_file)
    assert settings.test_mode is True
    assert settings.alert_email_to == ("a@example.test", "b@example.test")


def test_redacting_filter_hides_secrets_in_messages_and_tracebacks():
    filt = RedactingFilter(["hunter2", "owner@example.test"])
    try:
        raise RuntimeError("bad password hunter2")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "login %s failed", ("owner@example.test",), exc_info=__import__("sys").exc_info())
    filt.filter(record)
    assert record.getMessage() == "login [REDACTED] failed"
    assert "hunter2" not in record.exc_text


def test_selector_overrides(tmp_path):
    path = tmp_path / "selectors.json"
    path.write_text('{"month_title": ".month"}')
    assert load_selectors(path).month_title == ".month"
    path.write_text('{"no_such_key": 1}')
    with pytest.raises(ConfigError, match="no_such_key"):
        load_selectors(path)
    assert load_selectors(None) == Selectors()


def test_slot_format_label_and_order():
    slot = Slot.parse("2026-10-14 pm")
    assert slot == Slot(dt.date(2026, 10, 14), Half.PM)
    assert str(slot) == "2026-10-14 PM"
    assert slot.label() == "Wed Oct 14 PM"
    assert sorted([slot, Slot.parse("2026-10-14 AM")])[0].half is Half.AM
