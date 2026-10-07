from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


class _Env:
    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def text(self, name: str, default: str | None = None) -> str | None:
        value = self.env.get(name)
        if value is None or not value.strip():
            return default
        return value.strip()

    def secret(self, name: str) -> str | None:
        value = self.env.get(name)
        if value:
            return value
        path = self.text(f"{name}_FILE")
        if path:
            try:
                value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
            except OSError:
                raise ConfigError(f"{name}_FILE points to a file that cannot be read") from None
            return value or None
        return None

    def integer(self, name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
        raw = self.text(name)
        if raw is None:
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a whole number") from None
        if lo is not None and value < lo:
            raise ConfigError(f"{name} must be at least {lo}")
        if hi is not None and value > hi:
            raise ConfigError(f"{name} must be at most {hi}")
        return value

    def boolean(self, name: str, default: bool) -> bool:
        raw = self.text(name)
        if raw is None:
            return default
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name} must be true or false")

    def items(self, name: str) -> tuple[str, ...]:
        raw = self.text(name)
        if raw is None:
            return ()
        return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://www.sosi1.com"
    elisa_email: str | None = field(default=None, repr=False)
    elisa_password: str | None = field(default=None, repr=False)
    headless: bool = True
    timezone: str = "America/New_York"
    selectors_file: Path | None = None

    test_mode: bool = True
    apply_on_first_run: bool = False
    max_applies_per_day: int = 10
    max_attempts_per_slot: int = 2

    notify_missed: bool = True
    notify_skipped: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from: str | None = None
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    alert_email_to: tuple[str, ...] = ()

    data_dir: Path = Path("data")
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, env_file: Path | None = None) -> Settings:
        merged: dict[str, str] = {}
        if env_file is not None and env_file.exists():
            merged.update(read_env_file(env_file))
        merged.update(os.environ if env is None else env)
        e = _Env(merged)

        timezone = e.text("TIMEZONE", "America/New_York") or "America/New_York"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError("TIMEZONE must be an IANA zone name such as America/New_York") from None
        log_level = (e.text("LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        selectors_file = e.text("SELECTORS_FILE")

        return cls(
            base_url=(e.text("ELISA_BASE_URL", cls.base_url) or cls.base_url).rstrip("/"),
            elisa_email=e.secret("ELISA_EMAIL"),
            elisa_password=e.secret("ELISA_PASSWORD"),
            headless=e.boolean("HEADLESS", True),
            timezone=timezone,
            selectors_file=Path(selectors_file) if selectors_file else None,
            test_mode=e.boolean("TEST_MODE", True),
            apply_on_first_run=e.boolean("APPLY_ON_FIRST_RUN", False),
            max_applies_per_day=e.integer("MAX_APPLIES_PER_DAY", 10, lo=0),
            max_attempts_per_slot=e.integer("MAX_ATTEMPTS_PER_SLOT", 2, lo=1),
            notify_missed=e.boolean("NOTIFY_MISSED", True),
            notify_skipped=e.boolean("NOTIFY_SKIPPED", False),
            smtp_host=e.text("SMTP_HOST"),
            smtp_port=e.integer("SMTP_PORT", 587, lo=1, hi=65535),
            smtp_username=e.text("SMTP_USERNAME"),
            smtp_password=e.secret("SMTP_PASSWORD"),
            smtp_from=e.text("SMTP_FROM"),
            smtp_starttls=e.boolean("SMTP_STARTTLS", True),
            smtp_ssl=e.boolean("SMTP_SSL", False),
            alert_email_to=e.items("ALERT_EMAIL_TO"),
            data_dir=Path(e.text("DATA_DIR", "data") or "data"),
            log_level=log_level,
        )

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.alert_email_to)

    def secret_values(self) -> list[str]:
        candidates = [self.elisa_email, self.elisa_password, self.smtp_password]
        return [value for value in candidates if value]

    def require_site_credentials(self) -> None:
        missing = [
            name
            for name, value in (("ELISA_EMAIL", self.elisa_email), ("ELISA_PASSWORD", self.elisa_password))
            if not value
        ]
        if missing:
            raise ConfigError(f"missing required setting(s): {', '.join(missing)}")
