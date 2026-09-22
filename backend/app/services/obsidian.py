from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: F401 — keep import clear

from ..config import Settings, get_settings
from ..mcp.client import registry
from ..models.schemas import DailyNote, DailyNoteSection

TZ = ZoneInfo("Asia/Bangkok")

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


async def _client(settings: Settings):
    headers = {}
    if settings.obsidian_mcp_token:
        headers["Authorization"] = f"Bearer {settings.obsidian_mcp_token}"
    return await registry.get_http(
        "obsidian",
        settings.obsidian_mcp_url,
        headers=headers,
    )


def daily_note_path(day: date | None = None, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    day = day or datetime.now(TZ).date()
    # Format token D-YYYY-MM-DD → D-2026-09-13
    name = settings.obsidian_daily_format
    name = name.replace("YYYY", f"{day.year:04d}")
    name = name.replace("MM", f"{day.month:02d}")
    name = name.replace("DD", f"{day.day:02d}")
    if not name.endswith(".md"):
        name = f"{name}.md"
    return f"{settings.obsidian_daily_folder}/{name}"


def content_revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def parse_sections(content: str) -> list[DailyNoteSection]:
    matches = list(SECTION_RE.finditer(content))
    if not matches:
        return []
    sections: list[DailyNoteSection] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append(
            DailyNoteSection(
                heading=match.group(1).strip(),
                content=content[start:end].strip("\n"),
            )
        )
    return sections


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def instantiate_template(template: str, day: date) -> str:
    """Replace Templater tokens used in the Daily Template (best-effort)."""
    dt = datetime(day.year, day.month, day.day, tzinfo=TZ)
    iso_week = dt.isocalendar()
    week = f"{iso_week.year}-W{iso_week.week:02d}"
    month = f"{day.year:04d}-{day.month:02d}"
    quarter = f"Q{(day.month - 1) // 3 + 1} {day.year}"
    title = f"{dt.strftime('%A')}, {dt.strftime('%B')} {_ordinal(day.day)} {day.year}"

    replacements = {
        '<% tp.date.now("YYYY-MM-DD") %>': day.isoformat(),
        '<% tp.date.now("gggg-[W]WW") %>': week,
        '<% tp.date.now("YYYY-MM") %>': month,
        'Q<% Math.ceil(tp.date.now("M") / 3) %> <% tp.date.now("YYYY") %>': quarter,
        '<% Math.ceil(tp.date.now("M") / 3) %> <% tp.date.now("YYYY") %>': (
            f"{(day.month - 1) // 3 + 1} {day.year}"
        ),
        '<% tp.date.now("dddd, MMMM Do YYYY") %>': title,
        '<% tp.date.now("HH:mm") %>': datetime.now(TZ).strftime("%H:%M"),
        "<%tp.web.daily_quote() %>": "_Add a quote that resonates today._",
    }
    out = template
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    # Strip any leftover templater tags to avoid writing unevaluated code
    out = re.sub(r"<%[^%]*?%>", "", out)
    # Repair earlier QQ3-style typo if it slipped through
    out = re.sub(r"\bQQ(\d)\b", r"Q\1", out)
    return out


def repair_instantiated_note(content: str) -> str:
    """Fix known instantiation glitches in already-created daily notes."""
    return re.sub(r"\bQQ(\d)\b", r"Q\1", content)


def _extract_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("content", "text", "markdown", "data"):
            val = payload.get(key)
            if isinstance(val, str):
                return val
        if "result" in payload:
            return _extract_text(payload["result"])
    if isinstance(payload, list):
        parts = [_extract_text(x) for x in payload]
        return "\n".join(p for p in parts if p)
    return str(payload)


def _is_missing(payload: Any) -> bool:
    if payload is None:
        return True
    if isinstance(payload, str):
        low = payload.lower()
        return (
            low.startswith("file not found")
            or "not found" in low
            or low.startswith("error:")
        )
    if isinstance(payload, dict):
        err = str(payload.get("error") or payload.get("message") or "").lower()
        if "not found" in err:
            return True
    return False


async def _read_path(path: str) -> str | None:
    settings = get_settings()
    client = await _client(settings)
    try:
        raw = await client.call_tool("vault_read", {"path": path})
        if _is_missing(raw):
            return None
        text = _extract_text(raw)
        if not text or _is_missing(text):
            return None
        return text
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg or "does not exist" in msg:
            return None
        raise


async def _write_path(path: str, content: str) -> None:
    settings = get_settings()
    client = await _client(settings)
    await client.call_tool("vault_write", {"path": path, "content": content})


async def write_vault_note(path: str, content: str) -> None:
    await _write_path(path, content)


async def get_today_note(create: bool = True) -> DailyNote:
    settings = get_settings()
    day = datetime.now(TZ).date()
    path = daily_note_path(day, settings)
    existing = await _read_path(path)
    created = False
    if existing is None:
        if not create:
            raise FileNotFoundError(path)
        template = await _read_path(settings.obsidian_template_path)
        if template is None:
            raise RuntimeError(
                f"Daily template not found at {settings.obsidian_template_path}"
            )
        content = instantiate_template(template, day)
        await _write_path(path, content)
        created = True
    else:
        content = repair_instantiated_note(existing)
        # If the vault still holds unevaluated Templater tags, rebuild from template.
        if "<%" in content:
            template = await _read_path(settings.obsidian_template_path)
            if template is not None:
                content = instantiate_template(template, day)
                await _write_path(path, content)
                created = True
        elif content != existing:
            await _write_path(path, content)

    return DailyNote(
        path=path,
        date=day.isoformat(),
        content=content,
        revision=content_revision(content),
        created=created,
        sections=parse_sections(content),
    )


async def update_today_note(content: str, revision: str) -> DailyNote:
    settings = get_settings()
    day = datetime.now(TZ).date()
    path = daily_note_path(day, settings)
    current = await _read_path(path)
    if current is None:
        raise FileNotFoundError(path)
    current_rev = content_revision(current)
    if current_rev != revision:
        # Idempotent retry: same bytes already on disk
        if current == content:
            return DailyNote(
                path=path,
                date=day.isoformat(),
                content=current,
                revision=current_rev,
                created=False,
                sections=parse_sections(current),
            )
        raise ConflictError(current_rev, current)
    await _write_path(path, content)
    # Re-read so the revision matches whatever Obsidian actually stored
    stored = await _read_path(path)
    final = stored if stored is not None else content
    return DailyNote(
        path=path,
        date=day.isoformat(),
        content=final,
        revision=content_revision(final),
        created=False,
        sections=parse_sections(final),
    )


class ConflictError(Exception):
    def __init__(self, revision: str, content: str):
        self.revision = revision
        self.content = content
        super().__init__("Daily note was modified elsewhere")
