"""Dashboard-local timezone resolution.

Precedence: in-memory preference (from Postgres) → DASHBOARD_TIMEZONE env →
system IANA name when available → UTC.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import get_settings

logger = logging.getLogger(__name__)

_PREF_KEY = "timezone"
# None = not hydrated yet. "" = auto (no stored preference).
_preference: str | None = None
_loaded = False


def preference_key() -> str:
    return _PREF_KEY


def _system_timezone() -> str:
    """Best-effort IANA name for the host clock."""
    env_tz = (os.environ.get("TZ") or "").strip()
    if env_tz:
        try:
            ZoneInfo(env_tz)
            return env_tz
        except ZoneInfoNotFoundError:
            pass
    try:
        info = datetime.now().astimezone().tzinfo
        key = getattr(info, "key", None)
        if isinstance(key, str) and key:
            ZoneInfo(key)
            return key
    except (ZoneInfoNotFoundError, AttributeError, TypeError):
        pass
    return "UTC"


def configure_timezone(name: str | None) -> None:
    """Set or clear the runtime preference (empty/None → auto)."""
    global _preference, _loaded
    _loaded = True
    if name is None or not str(name).strip():
        _preference = ""
        return
    cleaned = str(name).strip()
    ZoneInfo(cleaned)  # validate
    _preference = cleaned


def timezone_name() -> str:
    if _preference:
        return _preference
    env = (get_settings().dashboard_timezone or "").strip()
    if env:
        try:
            ZoneInfo(env)
            return env
        except ZoneInfoNotFoundError:
            logger.warning("Invalid DASHBOARD_TIMEZONE=%r; falling back", env)
    return _system_timezone()


def get_tz() -> ZoneInfo:
    return ZoneInfo(timezone_name())


def timezone_source() -> str:
    if _preference:
        return "preference"
    if (get_settings().dashboard_timezone or "").strip():
        return "env"
    return "auto"


async def load_timezone_preference() -> None:
    """Hydrate from Postgres once at startup."""
    global _loaded
    if _loaded:
        return
    try:
        from .db import Preference, SessionLocal

        async with SessionLocal() as session:
            row = await session.get(Preference, _PREF_KEY)
            if row and row.value.strip():
                configure_timezone(row.value.strip())
            else:
                configure_timezone("")
    except Exception:  # noqa: BLE001
        logger.exception("Could not load timezone preference")
        _loaded = True


@lru_cache(maxsize=1)
def common_timezones() -> tuple[str, ...]:
    """Compact list for the UI when Intl.supportedValuesOf is unavailable."""
    return (
        "UTC",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Sao_Paulo",
        "Europe/London",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Zurich",
        "Asia/Dubai",
        "Asia/Kolkata",
        "Asia/Bangkok",
        "Asia/Singapore",
        "Asia/Tokyo",
        "Australia/Sydney",
        "Pacific/Auckland",
    )
