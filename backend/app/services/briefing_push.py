from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..models.schemas import Briefing
from . import pushover
from .hermes import MODEL_BRIEFING_SOURCES, is_failed_model_reply, local_now_context

logger = logging.getLogger(__name__)

MORNING_START_HOUR = 7
SKIP_PERIODS = frozenset({"night"})
_POLL_S = 5 * 60
_STARTUP_DELAY_S = 20
_SAME_RATIO = 0.92
_SAME_JACCARD = 0.88

_lock = asyncio.Lock()
_last_pushed_fp: str | None = None
_last_pushed_context_fp: str | None = None
_fingerprint_path_override: Path | None = None
_context_fingerprint_path_override: Path | None = None


def reset_state() -> None:
    global _last_pushed_fp, _last_pushed_context_fp
    _last_pushed_fp = None
    _last_pushed_context_fp = None


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.pushover_user_key.strip() and settings.pushover_api_key.strip())


def slot_for(now: dict[str, Any]) -> str | None:
    period = str(now.get("time_of_day") or "")
    if period in SKIP_PERIODS:
        return None
    hour = int(now.get("hour") or 0)
    if period == "morning" and hour < MORNING_START_HOUR:
        return None
    date = str(now.get("date") or "")
    if not date or not period:
        return None
    return f"{date}:{period}"


def _plain(summary: str) -> str:
    text = summary.strip()
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    return text


def fingerprint(summary: str) -> str:
    text = _plain(summary).lower()
    text = re.sub(r"[*_`>#~\-\[\]()]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def substantively_same(previous: str, current: str) -> bool:
    prev_fp = fingerprint(previous)
    new_fp = fingerprint(current)
    if not prev_fp or not new_fp:
        return False
    if prev_fp == new_fp:
        return True
    if SequenceMatcher(None, prev_fp, new_fp).ratio() >= _SAME_RATIO:
        return True
    prev_words = set(prev_fp.split())
    new_words = set(new_fp.split())
    if not prev_words or not new_words:
        return False
    return len(prev_words & new_words) / len(prev_words | new_words) >= _SAME_JACCARD


def _fingerprint_path() -> Path:
    if _fingerprint_path_override is not None:
        return _fingerprint_path_override
    return (
        Path.home()
        / "Library/Application Support/hermes-dashboard/last-pushed-fingerprint.txt"
    )


def _context_fingerprint_path() -> Path:
    if _context_fingerprint_path_override is not None:
        return _context_fingerprint_path_override
    return (
        Path.home()
        / "Library/Application Support/hermes-dashboard/last-pushed-context.txt"
    )


def _remember_pushed(summary: str, context_fp: str = "") -> None:
    global _last_pushed_fp, _last_pushed_context_fp
    text = fingerprint(summary)
    _last_pushed_fp = text
    path = _fingerprint_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("Failed to persist last pushed briefing fingerprint")
    context_fp = (context_fp or "").strip()
    _last_pushed_context_fp = context_fp
    if not context_fp:
        return
    ctx_path = _context_fingerprint_path()
    try:
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = ctx_path.with_name(ctx_path.name + ".tmp")
        tmp.write_text(context_fp + "\n", encoding="utf-8")
        tmp.replace(ctx_path)
    except OSError:
        logger.exception("Failed to persist last pushed briefing context fingerprint")


def _previous_pushed() -> str | None:
    global _last_pushed_fp
    if _last_pushed_fp is not None:
        return _last_pushed_fp or None
    try:
        _last_pushed_fp = _fingerprint_path().read_text(encoding="utf-8").strip()
    except OSError:
        _last_pushed_fp = ""
    return _last_pushed_fp or None


def _previous_context_fp() -> str | None:
    global _last_pushed_context_fp
    if _last_pushed_context_fp is not None:
        return _last_pushed_context_fp or None
    try:
        _last_pushed_context_fp = (
            _context_fingerprint_path().read_text(encoding="utf-8").strip()
        )
    except OSError:
        _last_pushed_context_fp = ""
    return _last_pushed_context_fp or None


def _unix(iso: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp())


def write_briefing_note(summary: str, path: str) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(summary.rstrip() + "\n", encoding="utf-8")
    tmp.replace(dest)


async def publish_briefing(
    briefing: Briefing,
    *,
    now: dict[str, Any] | None = None,
) -> bool:
    if briefing.source not in MODEL_BRIEFING_SOURCES:
        return False
    summary = (briefing.summary or "").strip()
    if is_failed_model_reply(summary):
        logger.warning("Skipping briefing publish for error/empty reply")
        return False
    now = now or local_now_context()
    settings = get_settings()
    context_fp = (briefing.context_fingerprint or "").strip()

    async with _lock:
        note_path = (settings.briefing_note_path or "").strip()
        if note_path:
            try:
                await asyncio.to_thread(write_briefing_note, summary, note_path)
            except OSError:
                logger.exception("Failed to write briefing note")

        if not is_configured():
            return bool(note_path)
        previous_ctx = _previous_context_fp()
        previous = _previous_pushed()
        if context_fp and previous_ctx:
            if context_fp == previous_ctx:
                logger.info("Skipping briefing push; Fantastical/OmniFocus unchanged")
                return True
            # Source changed — notify even when Apple rephrases almost the same way.
        elif previous is not None and substantively_same(previous, summary):
            logger.info("Skipping briefing push; no substantive change")
            if context_fp:
                _remember_pushed(summary, context_fp)
            return True
        title = f"Hermes Briefing · {now.get('weekday', '')} {now.get('time_of_day', '')}".strip()
        sent = await pushover.send_message(
            title,
            pushover.clip(_plain(summary), pushover.MESSAGE_LIMIT),
            timestamp=_unix(briefing.generated_at),
        )
        if sent:
            _remember_pushed(summary, context_fp)
        return sent


async def briefing_context(*, force: bool = False) -> dict[str, Any]:
    """Calendar and On Deck snapshot for a briefing.

    A failed calendar read is flagged instead of being passed on as an empty
    day. Callers must not ask Hermes to brief that snapshot.

    Pass ``force=True`` on explicit Refresh so a stale empty On Deck cache
    (from an earlier evening look-ahead) cannot keep the briefing on tomorrow.
    """
    from . import fantastical, omnifocus

    now = local_now_context()
    try:
        calendar = [e.model_dump() for e in await fantastical.get_today()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Briefing calendar unavailable: %s", exc)
        return {"calendar_unavailable": True, "error": str(exc), "now": now}
    try:
        tasks = [
            t.model_dump() for t in await omnifocus.get_on_deck(force=force)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Briefing tasks unavailable: %s", exc)
        # Prefer a recent On Deck cache over pretending the deck is empty —
        # empty would incorrectly flip the briefing to tomorrow.
        cached = omnifocus.cached_on_deck()
        tasks = [t.model_dump() for t in cached] if cached is not None else []
    logger.info(
        "Briefing snapshot: %s calendar events, %s On Deck tasks",
        len(calendar),
        len(tasks),
    )
    return {"calendar": calendar, "tasks": tasks, "now": now}


async def push_due_briefing() -> None:
    now = local_now_context()
    from .hermes import generate_briefing, look_ahead_tomorrow, peek_briefing

    context = await briefing_context()
    calendar = [e for e in (context.get("calendar") or []) if isinstance(e, dict)]
    tasks = [t for t in (context.get("tasks") or []) if isinstance(t, dict)]
    if look_ahead_tomorrow(calendar, tasks, now):
        held = peek_briefing()
        if held is not None and held.horizon == "tomorrow":
            return
        await generate_briefing(context, force=False)
        return
    hour = int(now.get("hour") or 0)
    if hour >= 17:
        # On Deck still has work after 17:00 — keep today's briefing, not tomorrow.
        await generate_briefing(context, force=False)
        return
    if slot_for(now) is None:
        return
    await generate_briefing(context, force=False)


async def run_loop() -> None:
    logger.warning("Briefing publish loop enabled")
    await asyncio.sleep(_STARTUP_DELAY_S)
    while True:
        try:
            await push_due_briefing()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled briefing publish failed")
        await asyncio.sleep(_POLL_S)
