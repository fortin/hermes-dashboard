from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from ..localtime import get_tz
from ..config import get_settings
from . import hermes, obsidian, omnifocus
from .hermes import HermesUnavailable

logger = logging.getLogger(__name__)

LOG_DELIMITER = "━━━ GLADYS LOG ━━━"
_STATUS_RE = re.compile(r"^STATUS:\s*(done|blocked|failed)\s*$", re.I | re.M)
_STARTUP_DELAY_S = 60
AgentState = Literal["assign", "running", "done", "blocked", "failed"]


@dataclass(frozen=True)
class AgentTags:
    assign: str = "🤖 Gladys"
    running: str = "gladys-running"
    done: str = "gladys-done"
    blocked: str = "gladys-blocked"
    failed: str = "gladys-failed"

    @classmethod
    def from_settings(cls, settings: Any) -> AgentTags:
        return cls(
            assign=settings.omnifocus_agent_tag,
            running=settings.omnifocus_agent_running_tag,
            done=settings.omnifocus_agent_done_tag,
            blocked=settings.omnifocus_agent_blocked_tag,
            failed=settings.omnifocus_agent_failed_tag,
        )

    def family(self) -> tuple[str, ...]:
        return (self.assign, self.running, self.done, self.blocked, self.failed)

    def state_tag(self, state: AgentState) -> str:
        return {
            "assign": self.assign,
            "running": self.running,
            "done": self.done,
            "blocked": self.blocked,
            "failed": self.failed,
        }[state]


def _parse_planned(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=get_tz())
    return parsed.astimezone(get_tz())


def planned_is_due(planned: str | None, now: datetime) -> bool:
    if not planned or not str(planned).strip():
        return True
    parsed = _parse_planned(str(planned).strip())
    if parsed is None:
        return True
    return parsed <= now


def _tag_keys(tags: list[str]) -> set[str]:
    keys: set[str] = set()
    for raw in tags:
        text = raw.strip()
        if not text:
            continue
        keys.add(text.lower())
        keys.add(text.split(" / ")[-1].strip().lower())
    return keys


_STATES: tuple[AgentState, ...] = ("running", "blocked", "failed", "done")


def classify_state(tags: list[str], agent_tags: AgentTags) -> AgentState | None:
    keys = _tag_keys(tags)
    for state in _STATES:
        if agent_tags.state_tag(state).lower() in keys:
            return state
    if agent_tags.assign.lower() in keys:
        return "assign"
    return None


def next_tags(current: list[str], agent_tags: AgentTags, state: AgentState) -> list[str]:
    family = {name.lower() for name in agent_tags.family()}
    kept: list[str] = []
    for tag in current:
        leaf = tag.split(" / ")[-1].strip().lower()
        if tag.strip().lower() in family or leaf in family:
            continue
        kept.append(tag)
    return kept + [agent_tags.state_tag(state)]


def pick_eligible(
    tasks: list[Any],
    agent_tags: AgentTags,
    now: datetime,
) -> Any | None:
    for task in tasks:
        if getattr(task, "completed", False):
            continue
        state = classify_state(list(getattr(task, "tags", []) or []), agent_tags)
        if state == "running" or state == "done" or state is None:
            continue
        if state in ("blocked", "failed"):
            if agent_tags.assign.lower() not in _tag_keys(list(getattr(task, "tags", []) or [])):
                continue
        if not planned_is_due(getattr(task, "planned", None), now):
            continue
        return task
    return None


def eligible_tasks(tasks: list[Any], agent_tags: AgentTags, now: datetime) -> list[Any]:
    return [task for task in tasks if pick_eligible([task], agent_tags, now) is not None]


def _task_row(task: Any, agent_tags: AgentTags) -> dict[str, Any]:
    tags = list(getattr(task, "tags", []) or [])
    return {
        "id": getattr(task, "id", ""),
        "name": getattr(task, "name", ""),
        "planned": getattr(task, "planned", None),
        "tags": tags,
        "state": classify_state(tags, agent_tags),
    }


async def peek_queue(*, now: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    agent_tags = AgentTags.from_settings(settings)
    clock = now or datetime.now(get_tz())
    snapshot: dict[str, Any] = {
        "enabled": bool(getattr(settings, "omnifocus_agent_enabled", False)),
        "tag": agent_tags.assign,
        "poll_seconds": int(getattr(settings, "omnifocus_agent_poll_seconds", 900)),
        "hermes_busy": hermes.hermes_busy(),
        "tagged": [],
        "eligible": [],
        "next": None,
        "error": None,
    }
    if not snapshot["enabled"]:
        return snapshot
    try:
        tagged = await omnifocus.get_tagged_tasks(agent_tags.assign)
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot
    ready = eligible_tasks(tagged, agent_tags, clock)
    snapshot["tagged"] = [_task_row(t, agent_tags) for t in tagged]
    snapshot["eligible"] = [_task_row(t, agent_tags) for t in ready]
    if ready:
        snapshot["next"] = _task_row(ready[0], agent_tags)
    return snapshot


def split_note(note: str | None) -> tuple[str, str]:
    text = (note or "").strip()
    if LOG_DELIMITER not in text:
        return text, ""
    original, previous = text.split(LOG_DELIMITER, 1)
    return original.strip(), previous.strip()


def parse_status(reply: str) -> AgentState:
    text = (reply or "").strip()
    if not text or hermes.is_failed_model_reply(text):
        return "failed"
    matches = list(_STATUS_RE.finditer(text))
    if matches:
        status = matches[-1].group(1).lower()
        if status in ("done", "blocked", "failed"):
            return status  # type: ignore[return-value]
    return "done"


def _short_receipt(reply: str, limit: int = 280) -> str:
    text = _STATUS_RE.sub("", reply or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def outcome_message(name: str, status: AgentState, reply: str) -> tuple[str, str]:
    title = f"Gladys · {status}"
    task = (name or "untitled").strip() or "untitled"
    summary = _short_receipt(reply)
    body = f"{task}\n{summary}" if summary else task
    return title, body


async def notify_outcome(name: str, status: AgentState, reply: str) -> None:
    if status not in ("done", "blocked", "failed"):
        return
    try:
        from . import pushover

        title, message = outcome_message(name, status, reply)
        await pushover.send_message(title, message)
    except Exception:
        logger.exception("Gladys push failed")


def format_receipt(body: str, *, claimed_at: str) -> str:
    return f"\n\n{LOG_DELIMITER}\nclaimed · {claimed_at}\n{(body or '').strip()}\n"


def build_prompt(name: str, original: str, previous: str) -> str:
    parts = [
        "Execute this OmniFocus task.",
        "",
        f"Task: {name.strip() or '(untitled)'}",
    ]
    if original:
        parts.extend(["", "Notes:", original])
    if previous:
        parts.extend(["", "Previous Gladys log:", previous])
    parts.extend(
        [
            "",
            "Do the work with your tools. If it should run later or repeat, create "
            "a Hermes cron job with the cronjob tool (deliver='telegram'). Then "
            "write a short receipt and end with exactly one line: STATUS: done, "
            "STATUS: blocked, or STATUS: failed.",
        ]
    )
    return "\n".join(parts)


def review_action_name(task_name: str) -> str:
    name = (task_name or "").strip() or "untitled"
    if name.lower().startswith("review:"):
        return name
    return f"Review: {name}"


def receipt_filename(task_name: str, clock: datetime) -> str:
    stem = re.sub(r'[\\/:*?"<>|]', "-", (task_name or "").strip()) or "untitled"
    stem = re.sub(r"\s+", " ", stem).strip(" .-")
    if len(stem) > 80:
        stem = stem[:80].rstrip()
    return f"{clock.strftime('%Y-%m-%d-%H%M')} {stem}.md"


def receipt_path(folder: str, filename: str) -> str:
    return f"{folder.strip().strip('/')}/{filename}"


def new_note_uid() -> str:
    return str(uuid.uuid4())


def advanced_obsidian_uri(
    vault: str,
    filepath: str | None = None,
    *,
    uid: str | None = None,
) -> str:
    """Build an Advanced URI. Prefer uid — filepath encoding mangling emoji folders."""
    if uid:
        return (
            "obsidian://adv-uri?"
            f"vault={quote(vault, safe='')}&"
            f"uid={quote(uid, safe='')}"
        )
    if not filepath:
        raise ValueError("advanced_obsidian_uri requires uid or filepath")
    return (
        "obsidian://adv-uri?"
        f"vault={quote(vault, safe='')}&"
        f"filepath={quote(filepath, safe='')}"
    )


def receipt_markdown(
    task_name: str,
    original: str,
    reply: str,
    *,
    claimed_at: str,
    uid: str,
) -> str:
    parts = [
        "---",
        f"uid: {uid}",
        "---",
        "",
        f"# {task_name.strip() or 'untitled'}",
        "",
        f"Claimed: {claimed_at}",
    ]
    if original.strip():
        parts.extend(["", "## Task", original.strip()])
    parts.extend(["", "## Receipt", (reply or "").strip(), ""])
    return "\n".join(parts)


async def record_success(
    task: Any,
    reply_text: str,
    *,
    claimed_at: str,
    clock: datetime,
    original: str = "",
) -> None:
    settings = get_settings()
    path = receipt_path(
        settings.obsidian_agent_receipt_folder,
        receipt_filename(getattr(task, "name", ""), clock),
    )
    uid = new_note_uid()
    await obsidian.write_vault_note(
        path,
        receipt_markdown(
            getattr(task, "name", ""),
            original,
            reply_text,
            claimed_at=claimed_at,
            uid=uid,
        ),
    )
    project_id = getattr(task, "project_id", None) or None
    project_name = None if project_id else (getattr(task, "project", None) or None)
    review_tag = settings.omnifocus_review_tag
    created = await omnifocus.add_task(
        review_action_name(getattr(task, "name", "")),
        note=advanced_obsidian_uri(settings.obsidian_vault_name, uid=uid),
        tags=[review_tag],
        defer_date=clock.isoformat(timespec="seconds"),
        project_id=project_id,
        project_name=project_name,
    )
    # OmniFocus copies the project's tags onto new actions. (Waiting) is
    # on-hold, so it would hide the review from On Deck until stripped.
    review_id = omnifocus.created_task_id(created)
    if review_id:
        await omnifocus.set_task_tags(review_id, [review_tag])


async def ensure_tags(agent_tags: AgentTags) -> None:
    try:
        existing = await omnifocus.list_tag_names()
    except Exception:
        logger.exception("Could not list OmniFocus tags")
        return
    try:
        if agent_tags.assign not in existing:
            await omnifocus.add_tag(agent_tags.assign)
            existing.add(agent_tags.assign)
        for child in (agent_tags.running, agent_tags.done, agent_tags.blocked, agent_tags.failed):
            if child not in existing:
                await omnifocus.add_tag(child, parent_name=agent_tags.assign)
        review = getattr(get_settings(), "omnifocus_review_tag", "🔎 Review")
        if review not in existing:
            await omnifocus.add_tag(review)
    except Exception:
        logger.exception("Could not ensure Gladys OmniFocus tags")


async def tick(*, now: datetime | None = None) -> dict[str, str] | None:
    settings = get_settings()
    if not getattr(settings, "omnifocus_agent_enabled", False):
        return None
    if hermes.hermes_busy():
        logger.warning("Skipping Gladys pickup; Hermes is busy")
        return None
    agent_tags = AgentTags.from_settings(settings)
    clock = now or datetime.now(get_tz())
    tasks = await omnifocus.get_tagged_tasks(agent_tags.assign)
    picked = pick_eligible(tasks, agent_tags, clock)
    ready = eligible_tasks(tasks, agent_tags, clock)
    logger.warning(
        "Gladys tick: tag=%s tagged=%s eligible=%s next=%s",
        agent_tags.assign,
        len(tasks),
        len(ready),
        getattr(picked, "name", None),
    )
    if picked is None:
        return None

    logger.warning("Gladys claiming %s (%s)", picked.name, picked.id)
    claimed_at = clock.isoformat(timespec="minutes")
    await omnifocus.set_task_tags(
        picked.id,
        next_tags(picked.tags, agent_tags, "running"),
    )
    original, previous = split_note(picked.note)
    prompt = build_prompt(picked.name, original, previous)
    try:
        reply = await hermes.execute_delegated_task(prompt)
        reply_text = reply.reply or ""
        status = parse_status(reply_text)
    except Exception as exc:
        if not isinstance(exc, HermesUnavailable):
            logger.exception("Gladys task %s failed", picked.id)
        reply_text = str(exc)
        status = "failed"

    await omnifocus.append_task_note(
        picked.id,
        format_receipt(reply_text, claimed_at=claimed_at),
    )
    await omnifocus.set_task_tags(
        picked.id,
        next_tags(picked.tags, agent_tags, status),
    )
    if status == "done":
        try:
            await record_success(
                picked,
                reply_text,
                claimed_at=claimed_at,
                clock=clock,
                original=original,
            )
        except Exception:
            logger.exception("Gladys success filing failed for %s", picked.id)
        await omnifocus.complete_task(picked.id)
    elif status == "blocked":
        await omnifocus.set_task_flagged(picked.id, True)
    await notify_outcome(getattr(picked, "name", "") or "", status, reply_text)
    logger.warning("Gladys finished %s as %s", picked.id, status)
    return {"id": picked.id, "status": status}


async def run_loop() -> None:
    settings = get_settings()
    if not settings.omnifocus_agent_enabled:
        logger.info("OmniFocus Gladys runner disabled")
        return
    logger.warning(
        "OmniFocus Gladys runner enabled (tag=%s, every %ss)",
        settings.omnifocus_agent_tag,
        settings.omnifocus_agent_poll_seconds,
    )
    await asyncio.sleep(_STARTUP_DELAY_S)
    await ensure_tags(AgentTags.from_settings(settings))
    poll = max(60, int(settings.omnifocus_agent_poll_seconds))
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gladys OmniFocus tick failed")
        await asyncio.sleep(poll)
