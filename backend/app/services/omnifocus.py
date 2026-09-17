from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import Settings, get_settings
from ..mcp.client import registry
from ..models.schemas import StatusCounts, TaskItem

logger = logging.getLogger(__name__)

# OmniFocus is driven via OmniJS automation — concurrent MCP hosts + frequent
# polls freeze the app. Cache reads and serialize all MCP calls from this process.
_OF_LOCK = asyncio.Lock()
_READ_TTL_S = 90.0
_on_deck_cache: tuple[float, list[TaskItem]] | None = None
_status_cache: tuple[float, StatusCounts] | None = None


async def _client(settings: Settings):
    return await registry.get_stdio(
        "omnifocus",
        settings.omnifocus_mcp_command,
    )


async def _call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
    settings = get_settings()
    async with _OF_LOCK:
        client = await _client(settings)
        return await client.call_tool(tool, arguments or {})


def invalidate_cache() -> None:
    global _on_deck_cache, _status_cache
    _on_deck_cache = None
    _status_cache = None


def _as_tasks(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # OmniFocus MCP wraps payloads as {ok, data: {items: [...]}}
        if "data" in payload and isinstance(payload["data"], (dict, list)):
            return _as_tasks(payload["data"])
        for key in ("tasks", "items", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        if "id" in payload and ("name" in payload or "title" in payload):
            return [payload]
    return []


def _normalize_task(raw: dict[str, Any]) -> TaskItem:
    tags = raw.get("tags") or raw.get("tagNames") or []
    if isinstance(tags, str):
        tags = [tags]
    project = (
        raw.get("project")
        or raw.get("projectName")
        or raw.get("project_name")
    )
    if isinstance(project, dict):
        project = project.get("name") or project.get("title")
    return TaskItem(
        id=str(raw.get("id") or raw.get("taskId") or ""),
        name=str(raw.get("name") or raw.get("title") or "Untitled"),
        completed=bool(
            raw.get("completed")
            or raw.get("completedAt")
            or str(raw.get("status", "")).lower() == "completed"
        ),
        flagged=bool(raw.get("flagged")),
        project=str(project) if project else None,
        due=raw.get("due") or raw.get("dueDate"),
        defer=raw.get("defer") or raw.get("deferDate"),
        planned=raw.get("planned") or raw.get("plannedDate"),
        estimated_minutes=raw.get("estimatedMinutes") or raw.get("estimated_minutes"),
        note=raw.get("note") or raw.get("notes"),
        tags=[str(t) for t in tags],
    )


async def get_on_deck(limit: int = 50, *, force: bool = False) -> list[TaskItem]:
    global _on_deck_cache
    if not force and _on_deck_cache is not None:
        cached_at, cached = _on_deck_cache
        if time.monotonic() - cached_at < _READ_TTL_S:
            return cached

    settings = get_settings()
    raw = await _call(
        "query_tasks",
        {
            "source": "custom",
            "perspective_name": settings.omnifocus_on_deck_perspective,
            "hide_completed": True,
            "limit": limit,
            "output": "compact",
            "sort_by": "library",
        },
    )
    tasks = [_normalize_task(t) for t in _as_tasks(raw) if t.get("id")]
    _on_deck_cache = (time.monotonic(), tasks)
    return tasks


async def get_tomorrow_actions(limit: int = 80) -> list[TaskItem]:
    """Actions from the named tomorrow perspective, as OmniFocus already filtered them."""
    settings = get_settings()
    perspective = settings.omnifocus_tomorrow_perspective
    raw = await _call(
        "query_tasks",
        {
            "source": "custom",
            "perspective_name": perspective,
            "hide_completed": True,
            "limit": limit,
            "output": "compact",
            "sort_by": "library",
        },
    )
    tasks = [_normalize_task(t) for t in _as_tasks(raw) if t.get("id")]
    logger.info("OmniFocus '%s': %s", perspective, len(tasks))
    return tasks


async def complete_task(task_id: str) -> dict[str, Any]:
    result = await _call(
        "complete_items",
        {"ids": [task_id], "action": "complete"},
    )
    invalidate_cache()
    return result


async def incomplete_task(task_id: str) -> dict[str, Any]:
    result = await _call(
        "complete_items",
        {"ids": [task_id], "action": "incomplete"},
    )
    invalidate_cache()
    return result


async def add_task(name: str, note: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"name": name}
    if note:
        args["note"] = note
    result = await _call("add_task", args)
    invalidate_cache()
    return result


def _extract_count(raw: Any) -> int | None:
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        for key in ("totalCount", "total", "count", "matching", "tasks"):
            if key in data and isinstance(data[key], int):
                return data[key]
        status = data.get("status") or data.get("by_status") or data.get("byStatus")
        if isinstance(status, dict) and status:
            return sum(int(v) for v in status.values() if isinstance(v, int))
        # omnifocus_status style headline counts
        counts = data.get("counts") or data.get("headline") or data.get("headlines")
        if isinstance(counts, dict):
            return None  # handled by caller
    if isinstance(raw, int):
        return raw
    return None


async def get_status_counts(*, force: bool = False) -> StatusCounts:
    """Prefer one omnifocus_status call; fall back to sparse counts."""
    global _status_cache
    if not force and _status_cache is not None:
        cached_at, cached = _status_cache
        if time.monotonic() - cached_at < _READ_TTL_S:
            return cached

    settings = get_settings()
    inbox = overdue = flagged = on_deck = None

    try:
        status_raw = await _call("omnifocus_status", {})
        data = status_raw.get("data") if isinstance(status_raw, dict) else None
        if not isinstance(data, dict):
            data = status_raw if isinstance(status_raw, dict) else {}
        counts = (
            data.get("counts")
            or data.get("headlineCounts")
            or data.get("headlines")
            or data.get("summary")
            or {}
        )
        if isinstance(counts, dict):
            inbox = counts.get("inbox") or counts.get("inboxCount")
            overdue = counts.get("overdue") or counts.get("overdueCount")
            flagged = counts.get("flagged") or counts.get("flaggedCount")
            on_deck = counts.get("onDeck") or counts.get("on_deck")
    except Exception:  # noqa: BLE001
        logger.debug("omnifocus_status unavailable; using count_tasks", exc_info=True)

    async def _count(source: str, **extra: Any) -> int | None:
        try:
            raw = await _call("count_tasks", {"source": source, **extra})
            return _extract_count(raw)
        except Exception:  # noqa: BLE001
            return None

    # Only fill missing fields — avoid 4 OmniJS round-trips when status worked
    if inbox is None:
        inbox = await _count("inbox")
    if overdue is None:
        overdue = await _count("overdue")
    if flagged is None:
        flagged = await _count("flagged")
    if on_deck is None:
        # Derive from cached On Deck list when possible
        if _on_deck_cache is not None and time.monotonic() - _on_deck_cache[0] < _READ_TTL_S:
            on_deck = len(_on_deck_cache[1])
        else:
            on_deck = await _count(
                "custom",
                perspective_name=settings.omnifocus_on_deck_perspective,
            )

    result = StatusCounts(
        inbox=inbox if isinstance(inbox, int) else None,
        overdue=overdue if isinstance(overdue, int) else None,
        flagged=flagged if isinstance(flagged, int) else None,
        on_deck=on_deck if isinstance(on_deck, int) else None,
    )
    _status_cache = (time.monotonic(), result)
    return result
