from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import get_settings
from ..models.schemas import AgentReply, Briefing

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Bangkok")

# Share one Hermes run at a time from this dashboard process so we don't
# exhaust gateway.max_concurrent_runs alongside other Hermes clients.
_hermes_lock = asyncio.Lock()
_briefing_lock = asyncio.Lock()
_BRIEFING_TTL_S = 5 * 60
_briefing_cache: tuple[float, Briefing] | None = None
_publish_tasks: set[asyncio.Task] = set()
_briefing_generation = 0
_hydrated_last_good = False
_last_good_path_override: Path | None = None
_nextday_cache: tuple[str, Briefing] | None = None
_hydrated_nextday = False
_nextday_path_override: Path | None = None
_DAY_DONE_HOUR = 17
_NEXTDAY_KIND = "perspective-v2"
_FAILED_REPLY_MARKERS = (
    "operation interrupted",
    "waiting for model response",
    "gateway is draining",
    "cannot reach hermes",
    "stream stale",
    "empty stream",
    "interrupted during api call",
)
# Hermes itself also has HERMES_API_TIMEOUT (see ~/.hermes/.env). Gladys uses a
# separate read timeout so overnight OmniFocus jobs are not cut at 30 minutes.
_HERMES_TIMEOUT = httpx.Timeout(connect=15.0, read=900.0, write=30.0, pool=15.0)
_TRANSIENT_HTTP = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_DELEGATED_SYSTEM = (
    "You are Gladys, Antonio's Hermes agent. He assigned you an OmniFocus task "
    "to execute in the background. Use your tools and do the work. Do not "
    "produce a daily briefing or tell him what he should do next.\n"
    "If the work should run later or repeat, schedule it with the cronjob tool "
    "(deliver='telegram' so results reach him). Then STATUS: done.\n"
    "When finished, write a short receipt: what changed, where artifacts live, "
    "and commit hashes if any. End with exactly one line: STATUS: done, "
    "STATUS: blocked, or STATUS: failed. If blocked, say what you need from him."
)


class HermesUnavailable(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def describe_http_error(exc: BaseException) -> str:
    """httpx/httpcore timeouts often have an empty str(); keep the type visible."""
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    cause = exc.__cause__
    if cause is not None:
        cause_text = str(cause).strip() or repr(cause)
        return f"{type(exc).__name__}: caused by {type(cause).__name__}: {cause_text}"
    return f"{type(exc).__name__}: no message"


def local_now_context() -> dict[str, Any]:
    now = datetime.now(TZ)
    hour = now.hour
    if hour < 12:
        period = "morning"
    elif hour < 17:
        period = "afternoon"
    elif hour < 21:
        period = "evening"
    else:
        period = "night"
    # Next Friday is the earliest Shabbat could start this week —
    # provided so the model cannot invent "Shabbat tonight" on a weekday.
    days_until_friday = (4 - now.weekday()) % 7
    next_friday = (now + timedelta(days=days_until_friday)).date()
    return {
        "timezone": "Asia/Bangkok",
        "iso": now.isoformat(timespec="minutes"),
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "human": now.strftime("%A %d %B %Y, %H:%M"),
        "time_of_day": period,
        "hour": hour,
        "minute": now.minute,
        "next_shabbat_starts_on": next_friday.isoformat(),
        # Fri after ~18:00 local as a coarse sunset proxy; Sat until evening.
        "is_shabbat": now.weekday() == 5 or (now.weekday() == 4 and hour >= 18),
    }


def _parse_local(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _clock_from_now(now: dict[str, Any]) -> datetime:
    iso = now.get("iso")
    if isinstance(iso, str):
        parsed = _parse_local(iso)
        if parsed is not None:
            return parsed
    return datetime.now(TZ)


def remaining_open_events(
    events: list[dict[str, Any]],
    clock: datetime,
) -> list[dict[str, Any]]:
    """Timed events that have not finished yet. All-day items are ignored."""
    open_events: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict) or raw.get("all_day"):
            continue
        start = _parse_local(str(raw.get("start") or ""))
        if start is None:
            continue
        end = _parse_local(str(raw.get("end") or ""))
        if end is not None and end <= clock:
            continue
        if start > clock or (end is not None and end > clock):
            open_events.append(raw)
    return open_events


def day_is_done(calendar: list[dict[str, Any]], now: dict[str, Any]) -> bool:
    """True once today's timed work is over (evening onward, nothing left on the clock)."""
    clock = _clock_from_now(now)
    if remaining_open_events(calendar, clock):
        return False
    hour = now.get("hour")
    if hour is None:
        hour = clock.hour
    return int(hour) >= _DAY_DONE_HOUR


_IGNORED_HOLIDAY_CALENDARS = frozenset(
    {
        "jewish holidays",
        "public holidays and observances",
        "holidays in the united kingdom",
        "holidays in united kingdom",
        "holidays in switzerland",
        "holidays in spain",
        "thai holidays",
        "us holidays",
    }
)
_THE_RE = re.compile(r"\bthe\b")
_SPACE_RE = re.compile(r"\s+")


def _calendar_key(name: str) -> str:
    text = _THE_RE.sub(" ", name.strip().lower())
    return _SPACE_RE.sub(" ", text).strip()


_IGNORED_HOLIDAY_KEYS = frozenset(
    _calendar_key(item) for item in _IGNORED_HOLIDAY_CALENDARS
)


def is_ignored_holiday_calendar(name: Any) -> bool:
    if not name:
        return False
    leaf = str(name).replace("\\", "/").split("/")[-1].split(":")[-1]
    key = _calendar_key(leaf)
    if key in _IGNORED_HOLIDAY_KEYS:
        return True
    return (
        key.startswith("holidays in ")
        or key.startswith("public holidays")
        or key.endswith(" holidays")
    )


def enrich_calendar_for_prompt(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add unambiguous local weekday/time labels so Hermes cannot invent schedules."""
    enriched: list[dict[str, Any]] = []
    for raw in events:
        if is_ignored_holiday_calendar(raw.get("calendar")):
            continue
        if "start_local" in raw and "weekday" in raw:
            enriched.append(raw)
            continue
        start = _parse_local(str(raw.get("start") or ""))
        end = _parse_local(str(raw.get("end") or ""))
        all_day = bool(raw.get("all_day"))
        if start and end and not all_day:
            span = end - start
            if span >= timedelta(hours=20) and start.hour == 0 and start.minute == 0:
                all_day = True
        enriched.append(
            {
                "title": raw.get("title"),
                "weekday": start.strftime("%A") if start else None,
                "date": start.strftime("%Y-%m-%d") if start else None,
                "start_local": (
                    "all day"
                    if all_day and start
                    else (start.strftime("%H:%M") if start else raw.get("start"))
                ),
                "end_local": (
                    None
                    if all_day
                    else (end.strftime("%H:%M") if end else raw.get("end"))
                ),
                "all_day": all_day,
                "calendar": raw.get("calendar") or None,
                "location": raw.get("location"),
            }
        )
    return enriched


def _datetime_rules(now: dict[str, Any]) -> str:
    return (
        f"AUTHORITATIVE CLOCK: today is {now['weekday']} {now['date']}, "
        f"local time {now['human']} ({now['timezone']}), "
        f"time of day = {now['time_of_day']}. "
        f"Shabbat for this locale next begins on Friday {now['next_shabbat_starts_on']} "
        f"(sunset Friday → dusk Saturday). "
        f"is_shabbat={now['is_shabbat']}. "
        "Rules you MUST follow:\n"
        "1. Do not invent weekday, date, or religious timing. Never say Shabbat is "
        "starting tonight unless is_shabbat is true or the clock is Friday evening.\n"
        "2. Holiday calendars are omitted from the snapshot (Jewish, Thai, UK, Swiss, "
        "Spanish, and generic public holidays). Do not mention those holidays, fasts, "
        "or observances, and do not treat them as Shabbat.\n"
        "3. Only mention calendar events listed in the snapshot, with their listed "
        "weekday and times. Do not add holidays or candle-lighting times that are "
        "not listed.\n"
        "4. Match tone to the actual time_of_day; never write a morning briefing at night."
    )


def hermes_busy() -> bool:
    return _hermes_lock.locked()


def delegated_task_timeout() -> httpx.Timeout:
    """Read timeout for Gladys OmniFocus jobs. 0/negative means no read deadline."""
    seconds = int(getattr(get_settings(), "omnifocus_agent_timeout_seconds", 0) or 0)
    read: float | None = None if seconds <= 0 else float(seconds)
    return httpx.Timeout(connect=15.0, read=read, write=30.0, pool=15.0)


async def execute_delegated_task(message: str) -> AgentReply:
    """Long-running OmniFocus assignment. Shares the gateway lock with Ask Hermes."""
    now = local_now_context()
    return await ask_hermes(
        message,
        include_snapshot=False,
        max_attempts=2,
        system=f"{_DELEGATED_SYSTEM}\n\n{_datetime_rules(now)}",
        timeout=delegated_task_timeout(),
    )


async def ask_hermes(
    message: str,
    context: dict[str, Any] | None = None,
    *,
    max_attempts: int = 5,
    include_snapshot: bool = True,
    system: str | None = None,
    timeout: httpx.Timeout | None = None,
) -> AgentReply:
    settings = get_settings()
    now = local_now_context()
    snapshot = dict(context or {})
    if "calendar" in snapshot and isinstance(snapshot["calendar"], list):
        snapshot["calendar"] = enrich_calendar_for_prompt(
            [e for e in snapshot["calendar"] if isinstance(e, dict)]
        )
    snapshot["now"] = now

    if system is None:
        system = (
            "You are Hermes embedded in Antonio's local day dashboard. "
            "Prefer concrete, actionable advice. Keep replies concise unless asked "
            "for detail.\n\n"
            f"{_datetime_rules(now)}\n"
            "Trust the dashboard snapshot's now + calendar over any other sense of "
            "what day it is. If tools disagree with the snapshot clock, the snapshot wins."
        )
    if include_snapshot:
        system += "\n\nCurrent dashboard snapshot (JSON):\n" + json.dumps(
            snapshot, default=str
        )[:8000]

    headers = {
        "Authorization": f"Bearer {settings.hermes_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.hermes_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "stream": False,
    }

    last_error = "Hermes unavailable"
    wait = 2
    url = f"{settings.hermes_base_url.rstrip('/')}/chat/completions"
    for attempt in range(1, max_attempts + 1):
        async with _hermes_lock:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=timeout or _HERMES_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, json=payload)
            except _TRANSIENT_HTTP as exc:
                elapsed = time.monotonic() - started
                last_error = (
                    f"Cannot reach Hermes API at {settings.hermes_base_url} "
                    f"after {elapsed:.1f}s ({describe_http_error(exc)})"
                )
                logger.warning(
                    "Hermes connect failed (attempt %s/%s): %s",
                    attempt,
                    max_attempts,
                    last_error,
                )
                wait = min(2 ** attempt, 15)
            except httpx.HTTPError as exc:
                elapsed = time.monotonic() - started
                raise HermesUnavailable(
                    f"Cannot reach Hermes API at {settings.hermes_base_url} "
                    f"after {elapsed:.1f}s ({describe_http_error(exc)})"
                ) from exc
            else:
                if resp.status_code == 429:
                    last_error = f"Hermes API error 429: {resp.text[:500]}"
                    wait = min(2 ** attempt, 20)
                    logger.warning(
                        "Hermes 429 (attempt %s/%s); retrying in %ss",
                        attempt,
                        max_attempts,
                        wait,
                    )
                elif resp.status_code >= 400:
                    raise HermesUnavailable(
                        f"Hermes API error {resp.status_code}: {resp.text[:500]}"
                    )
                else:
                    data = resp.json()
                    try:
                        reply = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise HermesUnavailable(
                            f"Unexpected Hermes response: {data}"
                        ) from exc
                    text = reply or ""
                    if is_failed_model_reply(text):
                        last_error = text.strip()[:400] or "empty Hermes reply"
                        logger.warning(
                            "Hermes error reply (attempt %s/%s): %s",
                            attempt,
                            max_attempts,
                            last_error,
                        )
                        wait = min(2 ** attempt, 20)
                    else:
                        return AgentReply(
                            reply=text,
                            model=data.get("model") or settings.hermes_model,
                        )

        if attempt < max_attempts:
            await asyncio.sleep(wait)

    raise HermesUnavailable(last_error, retryable=True)


_JSON_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_raw_controls_in_json_strings(text: str) -> str:
    """Turn raw newlines/tabs inside JSON strings into valid escapes."""
    out: list[str] = []
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            escaped = _JSON_CONTROL_ESCAPES.get(ch)
            if escaped is not None:
                out.append(escaped)
                continue
            if ord(ch) < 32:
                out.append(f"\\u{ord(ch):04x}")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = json.loads(_escape_raw_controls_in_json_strings(text))
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("JSON was not an object", text, 0)
    return parsed


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return _loads_json_object(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise json.JSONDecodeError("No JSON object", text, 0)
    return _loads_json_object(m.group(0))


def _task_id(task: dict[str, Any]) -> str:
    return str(task.get("id") or "")


def _task_name(task: dict[str, Any]) -> str:
    return str(task.get("name") or "").strip()


def match_task_ids_in_text(text: str, tasks: list[dict[str, Any]]) -> list[str]:
    """Find On Deck task names mentioned in briefing prose, in mention order."""
    haystack = text.lower()
    occupied = [False] * len(haystack)
    hits: list[tuple[int, str]] = []
    for task in sorted(tasks, key=lambda t: len(_task_name(t)), reverse=True):
        name = _task_name(task)
        tid = _task_id(task)
        if not tid or len(name) < 5:
            continue
        needle = name.lower()
        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx < 0:
                break
            end = idx + len(needle)
            before = haystack[idx - 1] if idx > 0 else " "
            after = haystack[end] if end < len(haystack) else " "
            if before.isalnum() or after.isalnum() or any(occupied[idx:end]):
                start = idx + 1
                continue
            for i in range(idx, end):
                occupied[i] = True
            hits.append((idx, tid))
            break
    hits.sort()
    seen: set[str] = set()
    ordered: list[str] = []
    for _, tid in hits:
        if tid not in seen:
            seen.add(tid)
            ordered.append(tid)
    return ordered


def resolve_suggested_task_ids(
    raw_ids: Any,
    tasks: list[dict[str, Any]],
    summary: str,
    *,
    limit: int = 3,
) -> list[str]:
    known_ids = {_task_id(t): t for t in tasks if _task_id(t)}
    by_name = {_task_name(t).lower(): _task_id(t) for t in tasks if _task_id(t) and _task_name(t)}
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_ids, list):
        for item in raw_ids:
            if not isinstance(item, str):
                continue
            key = item.strip()
            tid = key if key in known_ids else by_name.get(key.lower())
            if tid and tid not in seen:
                seen.add(tid)
                out.append(tid)
            if len(out) >= limit:
                return out
    if not out:
        out = match_task_ids_in_text(summary, tasks)[:limit]
    return out


def parse_briefing_reply(
    reply: str,
    tasks: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Split a Hermes briefing into display prose + suggested On Deck ids."""
    summary = (reply or "").strip()
    raw_ids: Any = None
    try:
        parsed = _extract_json_object(summary)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        parsed_summary = parsed.get("summary")
        if isinstance(parsed_summary, str) and parsed_summary.strip():
            summary = parsed_summary.strip()
        raw_ids = parsed.get("suggested_task_ids") or parsed.get("suggested_tasks")
    return summary, resolve_suggested_task_ids(raw_ids, tasks, summary)


def pin_suggested_tasks(
    tasks: list[dict[str, Any]],
    suggested_ids: list[str],
) -> list[dict[str, Any]]:
    if not suggested_ids:
        return list(tasks)
    rank = {tid: i for i, tid in enumerate(suggested_ids)}
    pinned: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for task in tasks:
        if _task_id(task) in rank:
            pinned.append(task)
        else:
            rest.append(task)
    pinned.sort(key=lambda t: rank.get(_task_id(t), 0))
    return pinned + rest


def briefing_generation() -> int:
    return _briefing_generation


def is_failed_model_reply(text: str) -> bool:
    """True when Hermes returned an operational error as assistant text."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _FAILED_REPLY_MARKERS)


def last_good_path() -> Path:
    if _last_good_path_override is not None:
        return _last_good_path_override
    return (
        Path.home()
        / "Library/Application Support/hermes-dashboard/last-briefing.json"
    )


def nextday_path() -> Path:
    if _nextday_path_override is not None:
        return _nextday_path_override
    return (
        Path.home()
        / "Library/Application Support/hermes-dashboard/nextday-plan.json"
    )


def _usable_briefing(
    briefing: Briefing | None,
    *,
    allow_tomorrow: bool = False,
) -> Briefing | None:
    if briefing is None or briefing.source != "hermes":
        return None
    if is_failed_model_reply(briefing.summary):
        return None
    if briefing.horizon == "tomorrow" and not allow_tomorrow:
        return None
    return briefing


def _hydrate_last_good() -> None:
    global _briefing_cache, _hydrated_last_good
    if _hydrated_last_good:
        return
    _hydrated_last_good = True
    if _usable_briefing(_briefing_cache[1] if _briefing_cache else None) is not None:
        return
    try:
        raw = last_good_path().read_text(encoding="utf-8")
        briefing = Briefing.model_validate_json(raw)
    except (OSError, ValueError):
        return
    if _usable_briefing(briefing) is None:
        return
    _briefing_cache = (time.monotonic(), briefing)


def _save_last_good(briefing: Briefing) -> None:
    if _usable_briefing(briefing) is None:
        return
    path = last_good_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(briefing.model_dump_json(), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("Failed to persist last good briefing")


def _last_good_briefing() -> Briefing | None:
    _hydrate_last_good()
    if _briefing_cache is None:
        return None
    return _usable_briefing(_briefing_cache[1])


def _hydrate_nextday() -> None:
    global _nextday_cache, _hydrated_nextday
    if _hydrated_nextday:
        return
    _hydrated_nextday = True
    if _nextday_cache is not None:
        return
    try:
        payload = json.loads(nextday_path().read_text(encoding="utf-8"))
        if payload.get("kind") != _NEXTDAY_KIND:
            return
        date = str(payload.get("date") or "")
        briefing = Briefing.model_validate(payload.get("briefing"))
    except (OSError, ValueError, TypeError):
        return
    if not date or _usable_briefing(briefing, allow_tomorrow=True) is None:
        return
    _nextday_cache = (date, briefing)


def _save_nextday(date: str, briefing: Briefing) -> None:
    global _nextday_cache
    if _usable_briefing(briefing, allow_tomorrow=True) is None:
        return
    _nextday_cache = (date, briefing)
    path = nextday_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "kind": _NEXTDAY_KIND,
                    "date": date,
                    "briefing": briefing.model_dump(),
                },
                default=str,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        logger.exception("Failed to persist next-day briefing")


def _nextday_for(date: str) -> Briefing | None:
    _hydrate_nextday()
    if not date or _nextday_cache is None or _nextday_cache[0] != date:
        return None
    return _usable_briefing(_nextday_cache[1], allow_tomorrow=True)


def peek_briefing() -> Briefing | None:
    """Return a cached briefing without touching Fantastical or OmniFocus."""
    now = local_now_context()
    if int(now.get("hour") or 0) >= _DAY_DONE_HOUR:
        return _nextday_for(str(now.get("date") or ""))
    fresh = _fresh_cached_briefing(force=False)
    if fresh is not None:
        return fresh
    if hermes_busy():
        return _last_good_briefing()
    return None


def _served_cache(context: dict[str, Any], now: dict[str, Any]) -> Briefing | None:
    calendar = enrich_calendar_for_prompt(
        [e for e in (context.get("calendar") or []) if isinstance(e, dict)]
    )
    if day_is_done(calendar, now):
        return _nextday_for(str(now.get("date") or ""))
    return _fresh_cached_briefing(force=False)


def _tomorrow_label(now: dict[str, Any]) -> dict[str, str]:
    clock = _clock_from_now(now) + timedelta(days=1)
    return {
        "weekday": clock.strftime("%A"),
        "date": clock.strftime("%Y-%m-%d"),
        "human": clock.strftime("%A %d %B %Y"),
    }


async def _load_tomorrow_tasks() -> list[dict[str, Any]]:
    try:
        from . import omnifocus

        return [t.model_dump() for t in await omnifocus.get_tomorrow_actions()]
    except Exception:
        logger.exception("Failed to load tomorrow's OmniFocus perspective")
        return []


async def _load_tomorrow_calendar() -> list[dict[str, Any]]:
    try:
        from . import fantastical

        events = await fantastical.get_tomorrow()
        logger.info("Tomorrow calendar: %s events", len(events))
        return [e.model_dump() for e in events]
    except Exception:
        logger.exception("Failed to load tomorrow's calendar")
        return []


def _fresh_cached_briefing(*, force: bool) -> Briefing | None:
    _hydrate_last_good()
    if force or _briefing_cache is None:
        return None
    cached_at, cached = _briefing_cache
    if time.monotonic() - cached_at < _BRIEFING_TTL_S and _usable_briefing(cached):
        return cached
    return None


async def _publish_safe(briefing: Briefing) -> None:
    try:
        from .briefing_push import publish_briefing

        await publish_briefing(briefing)
    except Exception:
        logger.exception("Briefing publish failed")


def _spawn_publish(briefing: Briefing) -> None:
    task = asyncio.create_task(_publish_safe(briefing))
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)


async def generate_briefing(
    context: dict[str, Any],
    *,
    force: bool = False,
) -> Briefing:
    now = local_now_context()
    if not force:
        cached = _served_cache(context, now)
        if cached is not None:
            return cached
    async with _briefing_lock:
        return await _generate_briefing_locked(context, force=force, now=now)


_BRIEFING_LAYOUT = (
    "Write the same kind of briefing as before — full sentences, with reasons — "
    "but as short markdown paragraphs, not one run-on block and not a labelled "
    "inventory. Start with **weekday date** on its own line. Then 2-4 short "
    "paragraphs: timed commitments in order and where the free blocks are; "
    "best use of the main free block (name the task, due/defer/planned date, "
    "and why it fits or what it sets up); other realistic tasks, each with a "
    "reason. Bold recommended task names. A short bullet list is fine only for "
    "several sibling recommendations, and each bullet must still be a full "
    "sentence with a reason. Do not use labelled section headings. "
    "No preamble, no pep talk. Aim for 100-160 words."
)


def _briefing_prompt(
    now: dict[str, Any],
    cal: list[dict[str, Any]],
    task_rows: list[dict[str, str]],
    *,
    tomorrow: dict[str, str] | None,
    tomorrow_cal: list[dict[str, Any]] | None,
) -> str:
    json_shape = (
        "Return ONLY JSON with this shape:\n"
        '{"summary":"markdown prose in short paragraphs; escape newlines as \\n",'
        '"suggested_task_ids":["id"]}\n'
        "suggested_task_ids must be 1-3 ids copied exactly from the task "
        "list above, in the order Antonio should do them next. Use [] if none. "
        "Do not invent ids."
    )
    if tomorrow is None:
        return (
            f"{_datetime_rules(now)}\n\n"
            f"Today's calendar (authoritative):\n{json.dumps(cal, default=str)}\n\n"
            f"On Deck tasks (use these ids, do not invent):\n"
            f"{json.dumps(task_rows, default=str)[:6000]}\n\n"
            "Produce a short briefing for the next few hours from NOW — not a "
            "generic morning briefing and not a Friday/Shabbat briefing unless "
            "today really is Friday evening or Saturday. "
            "Cover: remaining free blocks, highest-value On Deck task still "
            "realistic before the next timed commitment, and one suggested focus. "
            "Do not mention Shabbat, candle lighting, or breaking a fast unless "
            "those times appear explicitly in the calendar list above for today.\n\n"
            f"{_BRIEFING_LAYOUT}\n\n"
            f"{json_shape}"
        )
    return (
        f"{_datetime_rules(now)}\n\n"
        f"Today's remaining timed calendar is empty. Write a look-ahead for "
        f"TOMORROW {tomorrow['weekday']} {tomorrow['date']} ({tomorrow['human']}), "
        "not a recap of today and not a briefing for the rest of tonight. "
        "It is correct to plan tomorrow morning even though it is evening now.\n\n"
        f"Tomorrow's calendar (authoritative):\n"
        f"{json.dumps(tomorrow_cal or [], default=str)}\n\n"
        f"OmniFocus '{get_settings().omnifocus_tomorrow_perspective}' perspective "
        "(use these ids, do not invent; this is NOT On Deck):\n"
        f"{json.dumps(task_rows, default=str)[:6000]}\n\n"
        "Cover: first timed calendar commitment, then the highest-value "
        "action from that list that fits the morning. Mention due/defer/"
        "planned dates when they appear. Do not say On Deck is empty as if there "
        "is no work tomorrow. Name tomorrow's weekday. Do not say those events "
        "are today. Do not mention Shabbat, candle lighting, or breaking a fast "
        "unless those times appear explicitly in tomorrow's calendar list.\n\n"
        f"{_BRIEFING_LAYOUT}\n\n"
        f"{json_shape}"
    )


async def _generate_briefing_locked(
    context: dict[str, Any],
    *,
    force: bool,
    now: dict[str, Any],
) -> Briefing:
    global _briefing_cache, _briefing_generation
    if not force:
        cached = _served_cache(context, now)
        if cached is not None:
            return cached

    cal = enrich_calendar_for_prompt(
        [e for e in (context.get("calendar") or []) if isinstance(e, dict)]
    )
    tasks = [t for t in (context.get("tasks") or []) if isinstance(t, dict)]
    plan_tomorrow = day_is_done(cal, now)
    tomorrow = _tomorrow_label(now) if plan_tomorrow else None
    tomorrow_cal: list[dict[str, Any]] | None = None
    if plan_tomorrow:
        if "calendar_tomorrow" in context:
            raw_tomorrow = [
                e for e in (context.get("calendar_tomorrow") or []) if isinstance(e, dict)
            ]
        else:
            raw_tomorrow = await _load_tomorrow_calendar()
        tomorrow_cal = enrich_calendar_for_prompt(raw_tomorrow)
        if "tasks_tomorrow" in context:
            tasks = [
                t for t in (context.get("tasks_tomorrow") or []) if isinstance(t, dict)
            ]
        else:
            tasks = await _load_tomorrow_tasks()
    task_rows = [
        {
            "id": _task_id(t),
            "name": _task_name(t),
            **{
                key: t.get(key)
                for key in ("due", "defer", "planned")
                if t.get(key)
            },
        }
        for t in tasks
        if _task_id(t) and _task_name(t)
    ]
    prompt = _briefing_prompt(
        now, cal, task_rows, tomorrow=tomorrow, tomorrow_cal=tomorrow_cal
    )
    try:
        reply = await ask_hermes(
            prompt,
            context={
                "calendar": cal,
                "calendar_tomorrow": tomorrow_cal or [],
                "now": now,
                "tasks": task_rows,
                "horizon": "tomorrow" if plan_tomorrow else "today",
            },
            include_snapshot=False,
            max_attempts=2 if _last_good_briefing() is not None else 5,
        )
        summary, suggested = parse_briefing_reply(reply.reply, tasks)
        if is_failed_model_reply(summary):
            raise HermesUnavailable(summary[:400])
        briefing = Briefing(
            summary=summary,
            generated_at=datetime.now(TZ).isoformat(),
            source="hermes",
            suggested_task_ids=suggested,
            horizon="tomorrow" if plan_tomorrow else "today",
        )
        _briefing_cache = (time.monotonic(), briefing)
        _briefing_generation += 1
        if plan_tomorrow:
            _save_nextday(str(now.get("date") or ""), briefing)
        else:
            _save_last_good(briefing)
        _spawn_publish(briefing)
        return briefing
    except HermesUnavailable as exc:
        logger.warning("Briefing Hermes failed: %s", exc)
        if plan_tomorrow:
            planned = _nextday_for(str(now.get("date") or ""))
            if planned is not None:
                return planned
            logger.warning("Briefing Hermes failed (%s); not substituting today's briefing", exc)
            return Briefing(
                summary="Hermes is offline. Waiting for the gateway to come back.",
                generated_at=datetime.now(TZ).isoformat(),
                source="fallback",
                suggested_task_ids=[str(tasks[0]["id"])] if tasks and tasks[0].get("id") else [],
            )
        last = _last_good_briefing()
        if last is not None:
            logger.warning("Briefing Hermes failed (%s); returning last good", exc)
            _briefing_cache = (time.monotonic(), last)
            return last

        return Briefing(
            summary="Hermes is offline. Waiting for the gateway to come back.",
            generated_at=datetime.now(TZ).isoformat(),
            source="fallback",
            suggested_task_ids=[str(tasks[0]["id"])] if tasks and tasks[0].get("id") else [],
        )
