from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..localtime import get_tz

logger = logging.getLogger(__name__)


def _vault_root() -> Path | None:
    raw = (get_settings().obsidian_vault_path or "").strip()
    if not raw:
        return None
    return Path(raw)


def _completed_tasks_from_note(content: str) -> list[str]:
    tasks: list[str] = []
    for line in content.splitlines():
        m = re.match(r"^\s*[-*]\s+\[[xX]\]\s+(.+)$", line)
        if m:
            tasks.append(m.group(1).strip())
    return tasks


def _files_touched_today(*, created: bool, limit: int = 40) -> list[str]:
    """Best-effort filesystem scan for notes touched today (vault-relative paths)."""
    vault = _vault_root()
    if vault is None or not vault.exists():
        return []
    start = datetime.now(get_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = start.timestamp()
    hits: list[tuple[float, str]] = []
    skip_parts = {".obsidian", ".trash", "node_modules", ".git"}
    try:
        for path in vault.rglob("*.md"):
            if any(part in skip_parts for part in path.parts):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = st.st_ctime if created else st.st_mtime
            if stamp >= start_ts:
                rel = str(path.relative_to(vault))
                hits.append((stamp, rel))
    except OSError as exc:
        logger.warning("Vault scan failed: %s", exc)
        return []
    hits.sort(key=lambda x: x[0], reverse=True)
    return [rel for _, rel in hits[:limit]]


def resolve_dataview_block(query: str, note_content: str = "") -> dict[str, Any]:
    q = query.strip()
    low = q.lower()
    kind = "dataviewjs" if "dv.pages" in low or "dataviewjs" in low else "dataview"

    if "task" in low and "completed" in low:
        items = _completed_tasks_from_note(note_content)
        return {
            "kind": kind,
            "title": "Completed tasks (from this note)",
            "items": items,
            "empty": "No completed tasks in today’s note yet.",
            "approximate": True,
        }

    if "file.ctime" in low or "created today" in low:
        items = _files_touched_today(created=True)
        return {
            "kind": kind,
            "title": "Files created today",
            "items": items,
            "empty": "No new notes detected today.",
            "approximate": True,
        }

    if "file.mtime" in low or "modified today" in low:
        items = _files_touched_today(created=False)
        return {
            "kind": kind,
            "title": "Files modified today",
            "items": items,
            "empty": "No modified notes detected today.",
            "approximate": True,
        }

    preview = q.splitlines()[0][:120] if q else "Dataview"
    return {
        "kind": kind,
        "title": "Dataview",
        "items": [],
        "empty": (
            "This query runs inside Obsidian. Open the note there for live results."
        ),
        "query_preview": preview,
        "approximate": False,
    }


async def resolve_dataview_block_async(
    query: str, note_content: str = ""
) -> dict[str, Any]:
    return await asyncio.to_thread(resolve_dataview_block, query, note_content)
