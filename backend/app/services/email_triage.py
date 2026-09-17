from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..models.schemas import EmailTriageItem, EmailTriageResponse
from .hermes import HermesUnavailable, ask_hermes

logger = logging.getLogger(__name__)

ACCOUNTS = ("gmail", "icloud", "zoho")  # outlook needs Graph/OAuth; skip for now
PAGE_SIZE = 25
SEARCH_QUERY = "(not flag seen) or (flag flagged)"
_TRIAGE_TTL_S = 5 * 60
_triage_cache: tuple[float, EmailTriageResponse] | None = None
TZ = ZoneInfo("Asia/Bangkok")


async def _run(cmd: list[str], timeout: float = 45.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"timeout running {' '.join(cmd)}"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def _addr(people: Any) -> str:
    if not people:
        return ""
    if isinstance(people, list) and people:
        p = people[0]
        if isinstance(p, dict):
            name = p.get("name") or ""
            email = p.get("email") or ""
            return f"{name} <{email}>".strip() if name else email
        return str(p)
    return str(people)


def _has_flag(flags: Any, name: str) -> bool:
    if not isinstance(flags, list):
        return False
    needle = name.lower()
    for f in flags:
        if isinstance(f, dict) and str(f.get("iana") or "").lower() == needle:
            return True
        if isinstance(f, str) and needle in f.lower():
            return True
    return False


async def list_accounts() -> list[str]:
    code, out, err = await _run(["himalaya", "--json", "account", "list"])
    if code != 0:
        logger.warning("himalaya account list failed: %s", err or out)
        return list(ACCOUNTS)
    try:
        data = json.loads(out)
        names = [a["name"] for a in data.get("accounts", []) if a.get("name")]
        return [n for n in names if n in ACCOUNTS] or list(ACCOUNTS)
    except json.JSONDecodeError:
        return list(ACCOUNTS)


async def list_actionable(account: str, limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Only unread or flagged envelopes — matches Smart Filter-style attention."""
    code, out, err = await _run(
        [
            "himalaya",
            "--json",
            "envelope",
            "search",
            "-a",
            account,
            "-s",
            str(limit),
            "--",
            SEARCH_QUERY,
        ],
        timeout=60.0,
    )
    if code != 0:
        logger.warning("envelope search %s failed: %s", account, err or out)
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.warning("envelope search %s returned non-JSON: %s", account, out[:200])
        return []
    if isinstance(data, dict) and data.get("error"):
        logger.warning("envelope search %s error: %s", account, data.get("error"))
        return []

    rows = []
    for env in data.get("envelopes") or []:
        flags = env.get("flags")
        unread = not _has_flag(flags, "seen")
        flagged = _has_flag(flags, "flagged")
        if not (unread or flagged):
            continue
        rows.append(
            {
                "id": str(env.get("id")),
                "account": account,
                "subject": env.get("subject") or "(no subject)",
                "from": _addr(env.get("from")),
                "date": env.get("date") or "",
                "seen": not unread,
                "flagged": flagged,
                "unread": unread,
                "snippet": "",
            }
        )
    return rows


async def read_snippet(account: str, message_id: str, max_chars: int = 1200) -> str:
    code, out, err = await _run(
        [
            "himalaya",
            "--json",
            "message",
            "read",
            "-a",
            account,
            message_id,
        ],
        timeout=60.0,
    )
    if code != 0:
        return ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        text = out.strip()
        return text[:max_chars]
    # JSON shape varies; flatten text-ish fields
    text = json.dumps(data, ensure_ascii=False)
    # Prefer plain body if present
    for key in ("text", "body", "plain", "content"):
        if isinstance(data.get(key), str) and data[key].strip():
            text = data[key]
            break
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise
        return json.loads(m.group(0))


async def triage_inbox(force: bool = False) -> EmailTriageResponse:
    global _triage_cache
    if not force and _triage_cache is not None:
        cached_at, cached = _triage_cache
        if time.monotonic() - cached_at < _TRIAGE_TTL_S and cached.source == "hermes":
            return cached

    accounts = await list_accounts()
    envelopes: list[dict[str, Any]] = []
    for account in accounts:
        try:
            envelopes.extend(await list_actionable(account, limit=PAGE_SIZE))
        except Exception:  # noqa: BLE001
            logger.exception("Failed searching %s", account)

    # Newest first; keep the set bounded for Hermes
    envelopes = sorted(envelopes, key=lambda e: e.get("date") or "", reverse=True)[:30]

    # Snippets only for the top actionable messages
    for env in envelopes[:15]:
        try:
            env["snippet"] = await read_snippet(env["account"], env["id"])
        except Exception:  # noqa: BLE001
            env["snippet"] = ""

    prompt = (
        "Triage these messages for Antonio's day dashboard. "
        "They are already filtered to unread OR flagged only — do not ask for more mail. "
        "Ignore pure promotional/newsletter/noise unless action is required. "
        "Return ONLY JSON with shape:\n"
        '{"items":[{"id":"...","account":"...","priority":"high|medium|low|noise",'
        '"disposition":"urgent_reply|reply|action|waiting|reference|noise",'
        '"reason":"...","needs_reply":true,'
        '"draft_reply":"plain text reply or null"}]}\n'
        "Draft replies only when a human reply is actually warranted. "
        "Be concise. Do not invent facts."
    )

    try:
        reply = await ask_hermes(
            prompt,
            context={"emails": envelopes, "instruction": "email_triage"},
        )
        parsed = _extract_json_object(reply.reply)
        by_key = {(e["account"], e["id"]): e for e in envelopes}
        items: list[EmailTriageItem] = []
        for raw in parsed.get("items") or []:
            key = (raw.get("account"), str(raw.get("id")))
            base = by_key.get(key) or {}
            draft = raw.get("draft_reply")
            if isinstance(draft, str) and not draft.strip():
                draft = None
            items.append(
                EmailTriageItem(
                    id=str(raw.get("id") or base.get("id") or ""),
                    account=str(raw.get("account") or base.get("account") or ""),
                    subject=str(base.get("subject") or raw.get("subject") or ""),
                    sender=str(base.get("from") or raw.get("from") or ""),
                    date=str(base.get("date") or raw.get("date") or ""),
                    priority=str(raw.get("priority") or "medium"),
                    disposition=str(raw.get("disposition") or "reference"),
                    reason=str(raw.get("reason") or ""),
                    needs_reply=bool(raw.get("needs_reply") or draft),
                    draft_reply=draft,
                    snippet=(base.get("snippet") or "")[:280],
                )
            )
        # Drop empty ids
        items = [i for i in items if i.id and i.account]
        # Prefer non-noise first
        rank = {"high": 0, "medium": 1, "low": 2, "noise": 3}
        items.sort(key=lambda i: rank.get(i.priority, 9))
        result = EmailTriageResponse(
            items=items,
            source="hermes",
            generated_at=datetime.now(TZ).isoformat(),
        )
        _triage_cache = (time.monotonic(), result)
        return result
    except (HermesUnavailable, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Hermes triage failed, using heuristic: %s", exc)
        if _triage_cache is not None and _triage_cache[1].source == "hermes":
            return _triage_cache[1]
        items = []
        for env in envelopes:
            subject = (env.get("subject") or "").lower()
            sender = (env.get("from") or "").lower()
            noise_hints = (
                "unsubscribe",
                "newsletter",
                "noreply",
                "no-reply",
                "digest",
                "invoice",
                "receipt",
                "onboarding",
            )
            is_noise = any(h in subject or h in sender for h in noise_hints)
            items.append(
                EmailTriageItem(
                    id=env["id"],
                    account=env["account"],
                    subject=env["subject"],
                    sender=env["from"],
                    date=env["date"],
                    priority="noise" if is_noise else "medium",
                    disposition="noise" if is_noise else "reference",
                    reason="Hermes busy — heuristic triage only",
                    needs_reply=False,
                    draft_reply=None,
                    snippet=env.get("snippet") or "",
                )
            )
        return EmailTriageResponse(items=items, source="fallback")


async def send_reply(account: str, message_id: str, body: str) -> dict[str, Any]:
    code, out, err = await _run(
        [
            "himalaya",
            "--json",
            "message",
            "reply",
            "-a",
            account,
            "--body",
            body,
            "--send",
            message_id,
        ],
        timeout=90.0,
    )
    if code != 0:
        raise RuntimeError(err.strip() or out.strip() or "Send failed")
    return {"ok": True, "detail": out.strip() or "sent"}


async def delete_message(account: str, message_id: str) -> dict[str, Any]:
    code, out, err = await _run(
        [
            "himalaya",
            "--json",
            "message",
            "delete",
            "-a",
            account,
            message_id,
        ],
        timeout=60.0,
    )
    if code != 0:
        raise RuntimeError(err.strip() or out.strip() or "Delete failed")
    return {"ok": True, "detail": out.strip() or "deleted"}
