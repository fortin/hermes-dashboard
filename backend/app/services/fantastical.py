from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..mcp.client import registry
from ..models.schemas import CalendarEvent

TZ = ZoneInfo("Asia/Bangkok")
_CAL_LOCK = asyncio.Lock()
_TODAY_TTL_S = 60.0
_today_cache: tuple[float, list[CalendarEvent]] | None = None


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _day_phrase(dt: datetime) -> str:
    local = _local(dt)
    return f"{local.strftime('%B')} {local.day} {local.year}"


def _parse_when(from_dt: datetime | None, to_dt: datetime | None) -> str:
    """Fantastical `when` is natural language, e.g. 'July 7 to July 10 2026'."""
    start = _local(
        from_dt
        or datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    end = _local(to_dt or (start + timedelta(days=1)))
    last = end
    if (last.hour, last.minute, last.second, last.microsecond) == (0, 0, 0, 0):
        last = last - timedelta(microseconds=1)
    if start.date() == last.date():
        return _day_phrase(start)
    if start.year == last.year:
        return (
            f"{start.strftime('%B')} {start.day} to "
            f"{last.strftime('%B')} {last.day} {last.year}"
        )
    return f"{_day_phrase(start)} to {_day_phrase(last)}"


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "events", "results", "calendarItems", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return [payload]
    return []


def _normalize_event(raw: dict[str, Any]) -> CalendarEvent | None:
    title = (
        raw.get("title")
        or raw.get("summary")
        or raw.get("name")
        or raw.get("description")
        or "Untitled"
    )
    start = (
        raw.get("start")
        or raw.get("startDate")
        or raw.get("start_date")
        or raw.get("date")
        or ""
    )
    end = raw.get("end") or raw.get("endDate") or raw.get("end_date") or start
    event_id = str(
        raw.get("id")
        or raw.get("identifier")
        or raw.get("uid")
        or f"{title}-{start}"
    )
    calendar = (
        raw.get("calendar")
        or raw.get("calendarName")
        or raw.get("calendar_name")
        or ""
    )
    if isinstance(calendar, dict):
        calendar = calendar.get("title") or calendar.get("name") or ""
    all_day = bool(
        raw.get("allDay")
        or raw.get("all_day")
        or raw.get("isAllDay")
        or False
    )
    # Hebcal / all-day items sometimes arrive as midnight→midnight without allDay
    if not all_day and start and end:
        try:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=TZ)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=TZ)
            if (
                end_dt - start_dt >= timedelta(hours=20)
                and start_dt.astimezone(TZ).hour == 0
                and start_dt.astimezone(TZ).minute == 0
            ):
                all_day = True
        except ValueError:
            pass
    location = raw.get("location")
    if isinstance(location, dict):
        location = location.get("title") or location.get("name")
    return CalendarEvent(
        id=event_id,
        title=str(title),
        start=str(start),
        end=str(end),
        calendar=str(calendar),
        location=str(location) if location else None,
        all_day=all_day,
        notes=raw.get("notes") or raw.get("note"),
        url=raw.get("url") or raw.get("fantasticalUrl"),
    )


async def _client(settings: Settings):
    return await registry.get_stdio(
        "fantastical",
        settings.fantastical_mcp_command,
    )


def _events_from_raw(raw: Any) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for item in _as_list(raw):
        # Fantastical may mix events and reminders; prefer event-like rows
        item_type = (item.get("type") or item.get("itemType") or "event").lower()
        if item_type in {"task", "reminder"} and not (
            item.get("start") or item.get("startDate")
        ):
            continue
        normalised = _normalize_event(item)
        if normalised:
            events.append(normalised)
    events.sort(key=lambda e: e.start)
    return events


async def _query_when(when: str) -> list[CalendarEvent]:
    settings = get_settings()
    async with _CAL_LOCK:
        client = await _client(settings)
        raw = await client.call_tool("queryCalendarItems", {"when": when})
    return _events_from_raw(raw)


async def get_events(
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> list[CalendarEvent]:
    return await _query_when(_parse_when(from_dt, to_dt))


async def get_today() -> list[CalendarEvent]:
    global _today_cache
    if _today_cache is not None:
        cached_at, cached = _today_cache
        if time.monotonic() - cached_at < _TODAY_TTL_S:
            return cached
    events = await _query_when("today")
    _today_cache = (time.monotonic(), events)
    return events


async def get_tomorrow() -> list[CalendarEvent]:
    return await _query_when("tomorrow")


async def get_upcoming(days: int = 7) -> list[CalendarEvent]:
    start = datetime.now(TZ)
    end = start + timedelta(days=days)
    return await get_events(start, end)
