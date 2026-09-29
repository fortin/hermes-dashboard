from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import get_settings
from ..models.schemas import AgentReply, Briefing, markdown_paragraphs, strip_leading_date

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Bangkok")

# Share one Hermes run at a time from this dashboard process so we don't
# exhaust gateway.max_concurrent_runs alongside other Hermes clients.
_hermes_lock = asyncio.Lock()
_apple_lock = asyncio.Lock()
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
_APPLE_MODEL = "apple-intelligence"
_APPLE_TIMEOUT_S = 180.0
# On-device Use Model shares a 4096-token window with the reply.
# A 2549-character briefing prompt was rejected; ~2400 still answered.
_APPLE_PROMPT_MAX = 2000
MODEL_BRIEFING_SOURCES = frozenset({"hermes", "apple", "schedule"})
_FAILED_REPLY_MARKERS = (
    "operation interrupted",
    "waiting for model response",
    "gateway is draining",
    "cannot reach hermes",
    "stream stale",
    "empty stream",
    "interrupted during api call",
    "api call failed",
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


def _local_on_date(
    date: Any,
    hhmm: Any,
    clock: datetime,
) -> datetime | None:
    if not date or not hhmm or hhmm == "all day":
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm).strip())
    if match is None:
        return None
    try:
        year, month, day = (int(part) for part in str(date).split("-"))
    except ValueError:
        return None
    return datetime(
        year,
        month,
        day,
        int(match.group(1)),
        int(match.group(2)),
        tzinfo=clock.tzinfo or TZ,
    )


def omit_finished_events(
    events: list[dict[str, Any]],
    clock: datetime,
) -> list[dict[str, Any]]:
    """Drop timed events that have already ended. Keep all-day and in-progress."""
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("all_day") or event.get("start_local") == "all day":
            kept.append(event)
            continue
        end = _parse_local(str(event.get("end") or "")) or _local_on_date(
            event.get("date"), event.get("end_local"), clock
        )
        if end is not None:
            if end > clock:
                kept.append(event)
            continue
        start = _parse_local(str(event.get("start") or "")) or _local_on_date(
            event.get("date"), event.get("start_local"), clock
        )
        if start is None or start > clock:
            kept.append(event)
    return kept


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


def _timed_events_remain(events: list[dict[str, Any]], now: dict[str, Any]) -> bool:
    clock = _clock_from_now(now)
    for event in omit_finished_events(events, clock):
        if event.get("all_day") or event.get("start_local") == "all day":
            continue
        return True
    return False


def _has_named_tasks(tasks: list[Any]) -> bool:
    return any(isinstance(task, dict) and _task_id(task) and _task_name(task) for task in tasks)


def look_ahead_tomorrow(
    calendar: list[dict[str, Any]],
    tasks: list[Any],
    now: dict[str, Any],
) -> bool:
    """Preview tomorrow once today's timed events and On Deck are both finished.

    Evening with nothing left on the clock still looks ahead even if On Deck
    has tasks, matching the previous Hermes horizon.
    """
    if day_is_done(calendar, now):
        return True
    return not _timed_events_remain(calendar, now) and not _has_named_tasks(tasks)


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
                        # The gateway already retried. Repeating a compute
                        # error here just pins the shared lock and wedges
                        # the local model.
                        break
                    else:
                        return AgentReply(
                            reply=text,
                            model=data.get("model") or settings.hermes_model,
                        )

        if attempt < max_attempts:
            await asyncio.sleep(wait)

    raise HermesUnavailable(last_error, retryable=True)


def siri_binary() -> Path:
    configured = (get_settings().siri_bin or "").strip()
    if configured:
        return Path(configured).expanduser()
    found = shutil.which("siri")
    if found:
        return Path(found)
    return Path.home() / ".local" / "bin" / "siri"


def _kill_apple_process(proc: asyncio.subprocess.Process) -> None:
    """Stop siri and the Shortcuts child. Killing the shell alone leaves the request open."""
    if proc.returncode is not None or proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()


async def ask_apple(message: str, *, timeout: float = _APPLE_TIMEOUT_S) -> AgentReply:
    """One-shot Apple Intelligence reply via `siri --raw`. No tools."""
    async with _apple_lock:
        return await _ask_apple_unlocked(message, timeout=timeout)


async def _ask_apple_unlocked(message: str, *, timeout: float) -> AgentReply:
    prompt = (message or "").strip()
    if not prompt:
        raise HermesUnavailable("Apple Intelligence prompt was empty")
    binary = siri_binary()
    env = os.environ.copy()
    env["SIRI_RENDER"] = "0"
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--shortcut",
            get_settings().siri_shortcut,
            "--raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HermesUnavailable(f"siri CLI not found at {binary}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        _kill_apple_process(proc)
        await proc.wait()
        raise HermesUnavailable(
            f"Apple Intelligence timed out after {timeout:.0f}s"
        ) from exc
    text = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not text:
        detail = err or text or f"exit {proc.returncode}"
        raise HermesUnavailable(f"Apple Intelligence failed: {detail[:500]}")
    if is_failed_model_reply(text):
        raise HermesUnavailable(text[:400] or "empty Apple Intelligence reply")
    return AgentReply(reply=text, model=_APPLE_MODEL)


_VAGUE_TASK_REF = re.compile(
    r"\b(?:"
    r"this task|that task|these (?:materials|tasks)|the task|"
    r"tackling it|completing (?:this|it)|revising these|"
    r"consider a couple|a couple of shorter tasks|"
    r"this will\b|that will\b|"
    r"although it(?:'s| is) slightly deferred"
    r")\b",
    re.IGNORECASE,
)
_INVENTORY_LABEL = re.compile(
    r"(?m)^(?:[-*]\s*)?.{2,90}\(\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?\):\s*",
)
_CAL_INVENTORY = re.compile(
    r"should be (?:taken|attended|done|completed) as (?:planned|scheduled)",
    re.IGNORECASE,
)
_MAX_NAMED_TASKS = 3


def _sentences(text: str):
    for line in (text or "").splitlines():
        for part in re.split(r"(?<=[.!?])\s+", line.strip()):
            if part:
                yield part


_DANGLING_OPENER = re.compile(
    r"^(?:it(?:'s|’s| is)?|this|that)\b",
    re.IGNORECASE,
)
_NAME_TOKEN = re.compile(r"[A-Za-z0-9]{4,}")
_NAME_STOP = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "your",
        "have",
        "been",
        "will",
        "before",
        "after",
        "about",
        "into",
        "over",
        "then",
        "when",
        "what",
        "they",
        "them",
        "should",
        "would",
        "could",
        "task",
        "tasks",
        "work",
        "time",
        "more",
        "than",
    }
)


def _task_match_tokens(name: str) -> set[str]:
    return {
        token
        for token in _NAME_TOKEN.findall(name.casefold())
        if token not in _NAME_STOP
    }


def _pick_dangling_name(
    sentence: str,
    preferred: list[str],
    pool: list[str],
    used: set[str],
) -> str | None:
    available = [name for name in pool if name not in used]
    folded = sentence.casefold()

    def score(name: str) -> int:
        return sum(1 for token in _task_match_tokens(name) if token in folded)

    best = max((score(name) for name in available), default=0)
    if not best:
        # No lexical overlap — don't glue a random On Deck name onto an
        # invented referent ("before the embassy closes").
        return None
    tied = [name for name in available if score(name) == best]
    for name in preferred:
        if name in tied:
            return name
    return tied[0]

def name_dangling_opener(
    summary: str,
    task_names: list[str],
    *,
    all_names: list[str] | None = None,
) -> str:
    """Name or drop opening sentences that never say which task they mean."""
    preferred = [name.strip() for name in task_names if name and name.strip()]
    pool = [
        name.strip()
        for name in (all_names if all_names is not None else preferred)
        if name and name.strip()
    ]
    for name in preferred:
        if name not in pool:
            pool.append(name)
    used: set[str] = set()
    still_opening = True
    paragraphs: list[str] = []
    for paragraph in (summary or "").split("\n\n"):
        parts: list[str] = []
        for part in _sentences(paragraph):
            dangling = bool(
                still_opening
                and _DANGLING_OPENER.search(part)
                and not _mentions_listed_task(part, pool)
            )
            if dangling:
                name = _pick_dangling_name(part, preferred, pool, used) if pool else None
                if name:
                    used.add(name)
                    parts.append(f"**{name}.** {part}")
                    continue
                # Invented referent ("before the embassy closes") with no listed
                # name to attach — drop the sentence rather than leave "it".
                continue
            still_opening = False
            if _mentions_listed_task(part, pool):
                for name in pool:
                    if name.casefold() in part.casefold():
                        used.add(name)
            parts.append(part)
        if parts:
            paragraphs.append(" ".join(parts))
    return "\n\n".join(paragraphs)


def _repair_cached_opener(briefing: Briefing, tasks: list[Any]) -> Briefing:
    global _briefing_cache, _nextday_cache
    by_id: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = _task_id(task)
        task_name = _task_name(task)
        if task_id and task_name:
            by_id[task_id] = task_name
    # Also harvest bold names already in the summary so nextday repairs work
    # without a fresh OmniFocus fetch of tomorrow's perspective.
    bold = re.findall(r"\*\*(.+?)\*\*", briefing.summary or "")
    ordered = [by_id[tid] for tid in briefing.suggested_task_ids if tid in by_id]
    all_names = list(dict.fromkeys([*ordered, *by_id.values(), *bold]))
    repaired = name_dangling_opener(
        briefing.summary, ordered or bold[:1], all_names=all_names
    )
    if repaired == briefing.summary:
        return briefing
    updated = briefing.model_copy(update={"summary": repaired})
    if _briefing_cache is not None and _briefing_cache[1] is briefing:
        _briefing_cache = (_briefing_cache[0], updated)
    if briefing.horizon == "tomorrow":
        if _nextday_cache is not None and _nextday_cache[1] is briefing:
            _nextday_cache = (_nextday_cache[0], updated)
        _save_nextday(
            (_nextday_cache[0] if _nextday_cache else str(local_now_context().get("date") or "")),
            updated,
        )
    else:
        _save_last_good(updated)
    return updated


def _apple_unusable_reason(
    text: str,
    task_names: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> str | None:
    """Why an Apple briefing cannot be shown, or None when it can."""
    body = text or ""
    lowered = body.lower()
    if lowered.count('{"summary"') >= 3:
        return "looped JSON"
    if any(
        phrase in lowered
        for phrase in (
            "json only",
            "instructions are not tasks",
            "do not invent holidays",
            "use only listed events",
        )
    ):
        return "echoed instructions"
    if len(_INVENTORY_LABEL.findall(body)) >= 3:
        return "inventory labels"
    if len(_CAL_INVENTORY.findall(body)) >= 2:
        return "calendar catalogue"
    names = [name for name in (task_names or []) if name.strip()]
    if names and _VAGUE_TASK_REF.search(body) and not _mentions_listed_task(body, names):
        return "vague task reference with no listed name"
    if names:
        mentioned = sum(1 for name in names if name.casefold() in body.casefold())
        if mentioned > _MAX_NAMED_TASKS:
            return f"named {mentioned} tasks"
    for task_id in task_ids or []:
        if task_id and len(task_id) >= 8 and task_id in body:
            return "raw task id in prose"
    return None


def apple_briefing_unusable(
    text: str,
    task_names: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> bool:
    """True when the on-device model echoed instructions, hedged names, or inventoried."""
    return _apple_unusable_reason(text, task_names, task_ids) is not None


def apple_briefing_prompt(
    now: dict[str, Any],
    events: list[dict[str, Any]],
    task_rows: list[dict[str, str]],
    *,
    tomorrow: dict[str, str] | None = None,
) -> str:
    """Compact on-device briefing prompt: triage, not inventory."""
    if tomorrow is not None:
        intro = (
            f"Tomorrow {tomorrow['weekday']} {tomorrow['date']} look-ahead "
            f"(now {now.get('weekday')} {now.get('time_of_day')}). "
            "Triage — do not list every item. Write 2-4 short paragraphs "
            "(~100-160 words). First sentence must name an event or bold a "
            "task — never open with It/This/That. Name what you pick; bold "
            "task names. Never put OmniFocus ids in the prose. No date opener, "
            "no pep talk, no 'Title (time): This is...' catalogue.\n"
            "Events:\n"
        )
        task_intro = (
            f"Tasks ({get_settings().omnifocus_tomorrow_perspective}):\n"
        )
        cover = (
            "Name the first timed commitment and the single best morning task "
            "(note due/defer/planned if shown, say why it fits). Mention at most "
            f"{_MAX_NAMED_TASKS} tasks total; leave the rest unmentioned."
        )
    else:
        intro = (
            f"{now.get('weekday')} {now.get('date')} {now.get('time_of_day')} "
            f"({now.get('timezone')}). Triage the next few hours — do not list "
            "every event or task. Write 2-4 short paragraphs (~100-160 words). "
            "Name what you pick in that same sentence; bold task names. "
            "Do not say 'this' or 'this will' unless the name is in that sentence. "
            "Never put OmniFocus ids in the "
            "prose. No date opener, no pep talk, no 'Title (time): This is...' "
            "catalogue.\n"
            "Events still ahead:\n"
        )
        task_intro = "On Deck tasks:\n"
        cover = (
            "Briefly place free blocks against timed commitments, then recommend "
            "the highest-value On Deck task that still fits before the next "
            "commitment (name it, note due/defer/planned, say why). Add at most "
            f"{_MAX_NAMED_TASKS - 1} more named tasks only if they fit the same "
            "window; leave everything else unmentioned."
        )
    if task_rows:
        footer = (
            f"{cover} Never invent events or tasks.\n"
            'Return JSON only: {"summary":"markdown paragraphs; use \\n",'
            '"suggested_task_ids":["id"]} with 1-3 ids from the list above.'
        )
        task_lines = [
            " | ".join(
                [str(row["name"])]
                + [
                    f"{key} {row[key]}"
                    for key in ("due", "defer", "planned")
                    if row.get(key)
                ]
                + [f"id {row['id']}"]
            )
            for row in task_rows
        ]
    else:
        footer = (
            "The task list is empty. Cover timed commitments and free blocks only "
            "in 1-3 short paragraphs. Do not name or invent any task.\n"
            'Return JSON only: {"summary":"markdown paragraphs; use \\n",'
            '"suggested_task_ids":[]}'
        )
        task_lines = []
    text = _fit_apple_prompt(
        intro,
        [_apple_event_line(event) for event in events],
        task_intro,
        task_lines,
        footer,
    )
    if len(text) > _APPLE_PROMPT_MAX:
        raise HermesUnavailable(
            f"Briefing prompt is {len(text)} chars; over the on-device limit"
        )
    return text


async def _ask_briefing(
    prompt: str,
    *,
    apple_prompt: str,
    max_attempts: int,
    task_names: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> AgentReply:
    if get_settings().apple_intelligence:
        try:
            reply = await ask_apple(apple_prompt)
            reason = _apple_unusable_reason(reply.reply, task_names, task_ids)
            if reason:
                logger.warning(
                    "Apple briefing unusable (%s): %.400s", reason, reply.reply
                )
                raise HermesUnavailable(
                    f"Apple Intelligence briefing was not usable ({reason})"
                )
            return reply
        except HermesUnavailable as exc:
            # A rejected draft is not worth a 15-minute Hermes wait when this
            # morning's briefing is still good. Transport failures still fall through.
            if "not usable" in str(exc) and _last_good_briefing() is not None:
                logger.warning(
                    "Briefing Apple Intelligence failed (%s chars, %s); keeping last good",
                    len(apple_prompt),
                    exc,
                )
                raise
            logger.warning(
                "Briefing Apple Intelligence failed (%s chars, %s); trying Hermes",
                len(apple_prompt),
                exc,
            )
    return await ask_hermes(
        prompt,
        include_snapshot=False,
        max_attempts=max_attempts,
    )


def apple_ask_prompt(message: str, context: dict[str, Any] | None) -> str:
    """Short day snapshot plus the question, capped for the on-device model."""
    snapshot = context or {}
    now = snapshot.get("now") if isinstance(snapshot.get("now"), dict) else None
    if not now:
        now = local_now_context()
    raw_cal = [e for e in (snapshot.get("calendar") or []) if isinstance(e, dict)]
    if raw_cal and "start_local" not in raw_cal[0]:
        raw_cal = enrich_calendar_for_prompt(raw_cal)
    tasks = [t for t in (snapshot.get("tasks") or []) if isinstance(t, dict)]
    question = (message or "").strip()
    intro = (
        f"{now.get('weekday')} {now.get('date')} {now.get('time_of_day')} "
        f"{now.get('timezone')}. Use only listed events and tasks.\n"
        "Events:\n"
    )
    text = _fit_apple_prompt(
        intro,
        [_apple_event_line(event) for event in raw_cal],
        "Tasks:\n",
        [
            _apple_task_line(task)
            for task in tasks
            if _task_id(task) and _task_name(task)
        ],
        f"Question: {question}\nAnswer concisely. Do not invent events, tasks, or times.",
    )
    if len(text) > _APPLE_PROMPT_MAX:
        raise HermesUnavailable(
            f"Ask prompt is {len(text)} chars; over the on-device limit"
        )
    return text


async def ask_day(
    message: str,
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AgentReply:
    """Ask about the day. Apple Intelligence first; Hermes if that call fails."""
    if get_settings().apple_intelligence:
        try:
            return await ask_apple(apple_ask_prompt(message, context))
        except HermesUnavailable as exc:
            logger.warning("Ask Apple Intelligence failed (%s); trying Hermes", exc)
    return await ask_hermes(message, context=context, **kwargs)


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
    summary = markdown_paragraphs(strip_leading_date(summary))
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
    if briefing is None or briefing.source not in MODEL_BRIEFING_SOURCES:
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
    held = _nextday_for(str(now.get("date") or ""))
    if held is not None:
        return held
    if int(now.get("hour") or 0) >= _DAY_DONE_HOUR:
        return None
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
    held = _nextday_for(str(now.get("date") or ""))
    if held is not None:
        return held
    tasks = [t for t in (context.get("tasks") or []) if isinstance(t, dict)]
    if look_ahead_tomorrow(calendar, tasks, now):
        return None
    open_cal = omit_finished_events(calendar, _clock_from_now(now))
    fp = context_fingerprint(open_cal, tasks, now, horizon="today")
    cached = _fresh_cached_briefing(force=False, context_fp=fp)
    if cached is None:
        return None
    return _repair_cached_opener(cached, tasks)


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


def context_fingerprint(
    events: list[dict[str, Any]],
    tasks: list[Any],
    now: dict[str, Any],
    *,
    horizon: str,
) -> str:
    """Stable hash of the Fantastical + OmniFocus rows that feed a briefing.

    Period is included so morning → afternoon can refresh when the calendar is
    unchanged. Wall-clock minutes are not, so Apple rewordings alone do not
    count as a new briefing.
    """
    event_rows: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_rows.append(
            {
                "id": str(event.get("id") or ""),
                "title": str(event.get("title") or ""),
                "start": str(event.get("start") or event.get("start_local") or ""),
                "end": str(event.get("end") or event.get("end_local") or ""),
                "all_day": bool(event.get("all_day")),
            }
        )
    event_rows.sort(key=lambda row: (row["start"], row["title"], row["id"]))
    task_rows: list[dict[str, str]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = _task_id(task)
        name = _task_name(task)
        if not task_id or not name:
            continue
        task_rows.append(
            {
                "id": task_id,
                "name": name,
                "due": str(task.get("due") or ""),
                "defer": str(task.get("defer") or ""),
                "planned": str(task.get("planned") or ""),
            }
        )
    task_rows.sort(key=lambda row: row["id"])
    payload = {
        "date": str(now.get("date") or ""),
        "period": str(now.get("time_of_day") or ""),
        "horizon": horizon,
        "events": event_rows,
        "tasks": task_rows,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fresh_cached_briefing(
    *,
    force: bool,
    context_fp: str | None = None,
) -> Briefing | None:
    _hydrate_last_good()
    if force or _briefing_cache is None:
        return None
    cached_at, cached = _briefing_cache
    if _usable_briefing(cached) is None:
        return None
    if context_fp:
        cached_fp = (cached.context_fingerprint or "").strip()
        if cached_fp and cached_fp == context_fp:
            return cached
        if cached_fp and cached_fp != context_fp:
            return None
    if time.monotonic() - cached_at < _BRIEFING_TTL_S:
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


def _remember_briefing(
    briefing: Briefing,
    *,
    plan_tomorrow: bool,
    today: str,
) -> Briefing:
    global _briefing_cache, _briefing_generation
    _briefing_cache = (time.monotonic(), briefing)
    _briefing_generation += 1
    if plan_tomorrow:
        _save_nextday(today, briefing)
    else:
        _save_last_good(briefing)
    _spawn_publish(briefing)
    return briefing


def _briefing_failure_summary(exc: BaseException) -> str:
    text = str(exc).lower()
    if "cannot reach" in text or "connect" in text:
        return "Hermes is offline. Waiting for the gateway to come back."
    if "compute error" in text or "api call failed" in text:
        return "The local model hit a compute error. The briefing will retry when it recovers."
    return "Hermes is unavailable. The briefing will retry shortly."


def _calendar_unavailable_briefing() -> Briefing:
    """Hold the last briefing when Fantastical did not answer.

    An empty list is a real empty day. A failed read must not be written up
    as one.
    """
    last = _last_good_briefing()
    if last is not None:
        return last
    return Briefing(
        summary="The calendar couldn't be loaded, so this briefing was held back.",
        generated_at=datetime.now(TZ).isoformat(),
        source="fallback",
    )


async def generate_briefing(
    context: dict[str, Any],
    *,
    force: bool = False,
) -> Briefing:
    if context.get("calendar_unavailable"):
        logger.warning("Skipping briefing; calendar snapshot unavailable")
        return _calendar_unavailable_briefing()
    now = local_now_context()
    if not force:
        cached = _served_cache(context, now)
        if cached is not None:
            return cached
    async with _briefing_lock:
        return await _generate_briefing_locked(context, force=force, now=now)


_BRIEFING_OPENING = (
    "Write the same kind of briefing as before — full sentences, with reasons — "
    "but as short markdown paragraphs, not one run-on block and not a labelled "
    "inventory. Separate paragraphs with a blank line. Do not open with the date. "
)

_BRIEFING_LAYOUT = (
    _BRIEFING_OPENING
    + "Write 2-4 short paragraphs: timed commitments in order and where the "
    "free blocks are; best use of the main free block (name a task from the "
    "list, its due/defer/planned date, and why it fits or what it sets up); "
    "other tasks from that list only, each with a reason. Bold recommended "
    "task names. A short bullet list is fine only for several of those tasks, "
    "and each bullet must still be a full sentence with a reason. Never invent "
    "a task, project, or inbox. Do not use labelled section headings. "
    "No preamble, no pep talk. Aim for 100-160 words."
)

_BRIEFING_LAYOUT_EMPTY = (
    _BRIEFING_OPENING
    + "The task list is empty. Write 1-3 short paragraphs on timed commitments "
    "and free blocks only. Do not name, suggest, or invent any task, project, "
    "inbox, or activity such as email. No bullet list. No preamble. Aim for "
    "40-80 words."
)


_UNLISTED_WORK = re.compile(
    r"\b(?:catch(?:ing)? up|pending work|ideal for|good time to|"
    r"inbox|project\s+\w|e-?mails?|brainstorm)\b",
    re.IGNORECASE,
)
_INVENTED_TASK_LEAD = re.compile(
    r"quick tasks|realistic tasks|tasks for this time|from your on deck|other tasks",
    re.IGNORECASE,
)
_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


def _mentions_listed_task(text: str, task_names: list[str]) -> bool:
    lowered = text.casefold()
    return any(name.casefold() in lowered for name in task_names if name.strip())


def _allowed_clock_times(
    events: list[dict[str, Any]],
    task_rows: list[dict[str, str]],
) -> set[str]:
    found: set[str] = set()
    for event in events:
        for key in ("start", "end", "start_local", "end_local"):
            found.update(_CLOCK_TIME.findall(str(event.get(key) or "")))
    for task in task_rows:
        for key in ("due", "defer", "planned"):
            found.update(_CLOCK_TIME.findall(str(task.get(key) or "")))
    return found


def _drop_invented_task_lines(
    summary: str,
    task_names: list[str] | None = None,
    allowed_times: set[str] | None = None,
) -> str:
    """Remove invented tasks, bullets, and clock times that were not listed."""
    names = task_names or []
    kept: list[str] = []
    for line in summary.splitlines():
        stripped = line.strip()
        if re.match(r"(?:[-*]|\d+[.)])\s+\S", stripped):
            if names and _mentions_listed_task(stripped, names):
                kept.append(line)
            continue
        if _INVENTED_TASK_LEAD.search(stripped) and not _mentions_listed_task(stripped, names):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        kept_sentences: list[str] = []
        for part in sentences:
            if not part:
                continue
            if _UNLISTED_WORK.search(part) and not _mentions_listed_task(part, names):
                continue
            if allowed_times is not None:
                mentioned = set(_CLOCK_TIME.findall(part))
                if mentioned - allowed_times:
                    continue
            if not names and re.search(r"\bnext task\b", part, re.I):
                continue
            kept_sentences.append(part)
        if kept_sentences:
            kept.append(" ".join(kept_sentences))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _factual_briefing(
    events: list[dict[str, Any]],
    task_rows: list[dict[str, str]],
    *,
    tomorrow: bool = False,
) -> str:
    if not events and not task_rows:
        if tomorrow:
            return "Nothing is on tomorrow's calendar, and that task list is empty."
        return "Nothing is left on today's calendar, and On Deck is empty."
    parts: list[str] = []
    if events:
        bits = []
        for event in events:
            title = event.get("title") or "Untitled"
            start = event.get("start_local")
            end = event.get("end_local")
            when = f"{start}–{end}" if start and end else (start or "")
            bits.append(f"{title} at {when}".strip() if when else str(title))
        parts.append("Still ahead: " + "; ".join(bits) + ".")
    if task_rows:
        parts.append("On Deck: " + "; ".join(str(row["name"]) for row in task_rows[:3]) + ".")
    return " ".join(parts)


def _apple_event_line(event: dict[str, Any]) -> str:
    start = event.get("start_local") or "?"
    end = event.get("end_local")
    when = start if not end else f"{start}-{end}"
    return f"{when} {event.get('title') or 'Untitled'}"


def _apple_task_line(task: dict[str, Any]) -> str:
    bits = [str(task.get("id") or ""), str(task.get("name") or "")]
    for key in ("due", "defer", "planned"):
        if task.get(key):
            bits.append(f"{key} {task[key]}")
    return " | ".join(bits)


def _fit_apple_prompt(
    intro: str,
    events: list[str],
    task_intro: str,
    tasks: list[str],
    footer: str,
) -> str:
    """Drop later events and tasks until the on-device prompt fits."""
    kept_events = list(events)
    kept_tasks = list(tasks)

    def render() -> str:
        notes: list[str] = []
        if len(kept_events) < len(events):
            notes.append(f"Showing first {len(kept_events)} of {len(events)} events.")
        if len(kept_tasks) < len(tasks):
            notes.append(f"Showing first {len(kept_tasks)} of {len(tasks)} tasks.")
        note = ("\n" + " ".join(notes)) if notes else ""
        event_body = "\n".join(kept_events) if kept_events else "(none)"
        task_body = "\n".join(kept_tasks) if kept_tasks else "(none)"
        return f"{intro}{event_body}{note}\n{task_intro}{task_body}\n{footer}"

    text = render()
    while len(text) > _APPLE_PROMPT_MAX and (kept_events or kept_tasks):
        if kept_tasks:
            kept_tasks.pop()
        else:
            kept_events.pop()
        text = render()
    return text


def _briefing_prompt(
    now: dict[str, Any],
    cal: list[dict[str, Any]],
    task_rows: list[dict[str, str]],
    *,
    tomorrow: dict[str, str] | None,
    tomorrow_cal: list[dict[str, Any]] | None,
) -> str:
    has_tasks = bool(task_rows)
    listed = json.dumps(task_rows, default=str)[:6000] if has_tasks else "(none)"
    calendar_body = json.dumps(cal, default=str) if cal else "(none)"
    tomorrow_body = json.dumps(tomorrow_cal or [], default=str) if tomorrow_cal else "(none)"
    listed_only = (
        "Do not mention an event, task, or clock time that is not listed above. "
        "Events that have already ended are omitted. "
    )
    layout = _BRIEFING_LAYOUT if has_tasks else _BRIEFING_LAYOUT_EMPTY
    if has_tasks:
        json_shape = (
            "Return ONLY JSON with this shape:\n"
            '{"summary":"markdown prose in short paragraphs; escape newlines as \\n",'
            '"suggested_task_ids":["id"]}\n'
            "suggested_task_ids must be 1-3 ids copied exactly from the task "
            "list above, in the order Antonio should do them next. Use [] if none. "
            "Do not invent ids."
        )
    else:
        json_shape = (
            "Return ONLY JSON with this shape:\n"
            '{"summary":"markdown prose in short paragraphs; escape newlines as \\n",'
            '"suggested_task_ids":[]}\n'
            "The task list is empty, so suggested_task_ids must be []."
        )
    if tomorrow is None:
        if has_tasks:
            cover = (
                "Cover: remaining free blocks, highest-value On Deck task still "
                "realistic before the next timed commitment, and one suggested focus. "
            )
        else:
            cover = (
                "On Deck is empty. Cover remaining free blocks only. "
                "Do not recommend any task. "
            )
        return (
            f"{_datetime_rules(now)}\n\n"
            f"Today's calendar (authoritative):\n{calendar_body}\n\n"
            f"On Deck tasks (use these ids, do not invent):\n{listed}\n\n"
            "Produce a short briefing for the next few hours from NOW — not a "
            "generic morning briefing and not a Friday/Shabbat briefing unless "
            "today really is Friday evening or Saturday. "
            f"{cover}"
            f"{listed_only}"
            "Do not mention Shabbat, candle lighting, or breaking a fast unless "
            "those times appear explicitly in the calendar list above for today.\n\n"
            f"{layout}\n\n"
            f"{json_shape}"
        )
    if has_tasks:
        cover = (
            "Cover: first timed calendar commitment, then the highest-value "
            "action from that list that fits the morning. Mention due/defer/"
            "planned dates when they appear. Do not say On Deck is empty as if there "
            "is no work tomorrow. "
        )
    else:
        cover = (
            "That perspective is empty. Cover tomorrow's timed commitments only. "
            "Do not recommend any task. "
        )
    return (
        f"{_datetime_rules(now)}\n\n"
        f"Today's remaining timed calendar is empty. Write a look-ahead for "
        f"TOMORROW {tomorrow['weekday']} {tomorrow['date']} ({tomorrow['human']}), "
        "not a recap of today and not a briefing for the rest of tonight. "
        "It is correct to plan tomorrow even though the clock is still today.\n\n"
        f"Tomorrow's calendar (authoritative):\n"
        f"{tomorrow_body}\n\n"
        f"OmniFocus '{get_settings().omnifocus_tomorrow_perspective}' perspective "
        "(use these ids, do not invent; this is NOT On Deck):\n"
        f"{listed}\n\n"
        f"{cover}"
        f"{listed_only}"
        "Name tomorrow's weekday. Do not say those events "
        "are today. Do not mention Shabbat, candle lighting, or breaking a fast "
        "unless those times appear explicitly in tomorrow's calendar list.\n\n"
        f"{layout}\n\n"
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
    logger.info(
        "Briefing calendar: %s",
        [event.get("title") for event in cal] or "empty",
    )
    tasks = [t for t in (context.get("tasks") or []) if isinstance(t, dict)]
    plan_tomorrow = look_ahead_tomorrow(cal, tasks, now)
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
    clock = _clock_from_now(now)
    open_cal = omit_finished_events(cal, clock)
    prompt_cal = cal if plan_tomorrow else open_cal
    listed_events = tomorrow_cal or [] if plan_tomorrow else open_cal
    fingerprint = context_fingerprint(
        listed_events,
        tasks,
        now,
        horizon="tomorrow" if plan_tomorrow else "today",
    )
    if not force:
        cached = _fresh_cached_briefing(force=False, context_fp=fingerprint)
        if cached is not None:
            return cached
    prompt = _briefing_prompt(
        now,
        prompt_cal,
        task_rows,
        tomorrow=tomorrow,
        tomorrow_cal=tomorrow_cal,
    )
    task_names = [str(row["name"]) for row in task_rows]
    task_ids = [str(row["id"]) for row in task_rows]
    try:
        try:
            apple_prompt = apple_briefing_prompt(
                now,
                listed_events,
                task_rows,
                tomorrow=tomorrow,
            )
        except HermesUnavailable:
            apple_prompt = prompt
        reply = await _ask_briefing(
            prompt,
            apple_prompt=apple_prompt,
            max_attempts=2 if _last_good_briefing() is not None else 5,
            task_names=task_names,
            task_ids=task_ids,
        )
        summary, suggested = parse_briefing_reply(reply.reply, tasks)
        summary = _drop_invented_task_lines(
            summary,
            task_names,
            _allowed_clock_times(listed_events, task_rows),
        )
        source = "apple" if reply.model == _APPLE_MODEL else "hermes"
        if not summary:
            summary = _factual_briefing(listed_events, task_rows, tomorrow=plan_tomorrow)
            source = "schedule"
            suggested = []
        elif not task_rows:
            suggested = []
        else:
            by_id = {str(row["id"]): str(row["name"]) for row in task_rows}
            summary = name_dangling_opener(
                summary,
                [by_id[tid] for tid in suggested if tid in by_id],
                all_names=list(by_id.values()),
            )
        if is_failed_model_reply(summary):
            raise HermesUnavailable(summary[:400])
        return _remember_briefing(
            Briefing(
                summary=summary,
                generated_at=datetime.now(TZ).isoformat(),
                source=source,
                suggested_task_ids=suggested,
                horizon="tomorrow" if plan_tomorrow else "today",
                context_fingerprint=fingerprint,
            ),
            plan_tomorrow=plan_tomorrow,
            today=str(now.get("date") or ""),
        )
    except HermesUnavailable as exc:
        unusable = "not usable" in str(exc)
        if unusable:
            logger.warning(
                "Briefing Apple Intelligence rejected (%s); returning last good", exc
            )
        else:
            logger.warning("Briefing Hermes failed: %s", exc)
        if plan_tomorrow:
            planned = _nextday_for(str(now.get("date") or ""))
            if planned is not None:
                return planned
            logger.warning("Briefing Hermes failed (%s); not substituting today's briefing", exc)
            return Briefing(
                summary=_briefing_failure_summary(exc),
                generated_at=datetime.now(TZ).isoformat(),
                source="fallback",
                suggested_task_ids=[str(tasks[0]["id"])] if tasks and tasks[0].get("id") else [],
            )
        last = _last_good_briefing()
        if last is not None:
            if not unusable:
                logger.warning("Briefing Hermes failed (%s); returning last good", exc)
            repaired = _repair_cached_opener(last, tasks)
            if unusable and fingerprint and repaired.context_fingerprint != fingerprint:
                repaired = repaired.model_copy(
                    update={"context_fingerprint": fingerprint}
                )
                _briefing_cache = (time.monotonic(), repaired)
                _save_last_good(repaired)
            else:
                _briefing_cache = (time.monotonic(), repaired)
            return repaired

        return Briefing(
            summary=_briefing_failure_summary(exc),
            generated_at=datetime.now(TZ).isoformat(),
            source="fallback",
            suggested_task_ids=[str(tasks[0]["id"])] if tasks and tasks[0].get("id") else [],
        )
