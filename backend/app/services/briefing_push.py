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
from .hermes import is_failed_model_reply, local_now_context

logger = logging.getLogger(__name__)

MORNING_START_HOUR = 7
SKIP_PERIODS = frozenset({"night"})
_POLL_S = 5 * 60
_STARTUP_DELAY_S = 20
_SAME_RATIO = 0.92
_SAME_JACCARD = 0.88

_lock = asyncio.Lock()
_last_pushed_fp: str | None = None
_fingerprint_path_override: Path | None = None


def reset_state() -> None:
    global _last_pushed_fp
    _last_pushed_fp = None


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


def _remember_pushed(summary: str) -> None:
    global _last_pushed_fp
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


def _previous_pushed() -> str | None:
    global _last_pushed_fp
    if _last_pushed_fp is not None:
        return _last_pushed_fp or None
    try:
        _last_pushed_fp = _fingerprint_path().read_text(encoding="utf-8").strip()
    except OSError:
        _last_pushed_fp = ""
    return _last_pushed_fp or None


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
    if briefing.source != "hermes":
        return False
    summary = (briefing.summary or "").strip()
    if is_failed_model_reply(summary):
        logger.warning("Skipping briefing publish for error/empty reply")
        return False
    now = now or local_now_context()
    settings = get_settings()

    async with _lock:
        note_path = (settings.briefing_note_path or "").strip()
        if note_path:
            try:
                await asyncio.to_thread(write_briefing_note, summary, note_path)
            except OSError:
                logger.exception("Failed to write briefing note")

        if not is_configured():
            return bool(note_path)
        previous = _previous_pushed()
        if previous is not None and substantively_same(previous, summary):
            logger.info("Skipping briefing push; no substantive change")
            return True
        title = f"Hermes Briefing · {now.get('weekday', '')} {now.get('time_of_day', '')}".strip()
        sent = await pushover.send_message(
            title,
            pushover.clip(_plain(summary), pushover.MESSAGE_LIMIT),
            timestamp=_unix(briefing.generated_at),
        )
        if sent:
            _remember_pushed(summary)
        return sent


async def _briefing_context() -> dict[str, Any]:
    from . import fantastical, omnifocus

    now = local_now_context()
    try:
        return {
            "calendar": [e.model_dump() for e in await fantastical.get_today()],
            "tasks": [t.model_dump() for t in await omnifocus.get_on_deck()],
            "now": now,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Briefing snapshot incomplete: %s", exc)
        return {"error": str(exc), "now": now}


async def push_due_briefing() -> None:
    now = local_now_context()
    from .hermes import generate_briefing, peek_briefing

    hour = int(now.get("hour") or 0)
    if hour >= 17:
        if peek_briefing() is not None:
            return
        await generate_briefing(await _briefing_context(), force=False)
        return
    if slot_for(now) is None:
        return
    await generate_briefing(await _briefing_context(), force=False)


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
