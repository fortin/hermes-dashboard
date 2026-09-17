from __future__ import annotations

import logging

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

# Official Message API: POST HTTPS to this URL with token, user, message.
# Source: https://pushover.net/api
MESSAGES_URL = "https://api.pushover.net/1/messages.json"
MESSAGE_LIMIT = 1024
TITLE_LIMIT = 250
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)

_halt_4xx = False


def reset_state() -> None:
    global _halt_4xx
    _halt_4xx = False


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.pushover_user_key.strip() and settings.pushover_api_key.strip())


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


async def send_message(
    title: str,
    message: str,
    *,
    timestamp: int | None = None,
) -> bool:
    """POST a Pushover notification. Returns True only on status=1.

    4xx responses are not retried (repeating invalid input will not work, and
    bursts of 4xx can get the IP blocked). 5xx may be retried by the caller.
    Source: https://pushover.net/api
    """
    global _halt_4xx
    if _halt_4xx:
        return False
    if not is_configured():
        return False

    settings = get_settings()
    payload = {
        "token": settings.pushover_api_key.strip(),
        "user": settings.pushover_user_key.strip(),
        "title": clip(title, TITLE_LIMIT),
        "message": clip(message, MESSAGE_LIMIT),
    }
    if timestamp is not None:
        payload["timestamp"] = str(timestamp)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(MESSAGES_URL, data=payload)
    except httpx.HTTPError as exc:
        logger.warning("Pushover request failed: %s", type(exc).__name__)
        return False

    if 400 <= resp.status_code < 500:
        _halt_4xx = True
        logger.error(
            "Pushover rejected the request (%s); not retrying until restart: %s",
            resp.status_code,
            resp.text[:300],
        )
        return False

    if resp.status_code >= 500:
        logger.warning("Pushover server error %s", resp.status_code)
        return False

    try:
        body = resp.json()
    except ValueError:
        logger.warning("Pushover returned non-JSON")
        return False

    if body.get("status") != 1:
        logger.error("Pushover status != 1: %s", body.get("errors") or body)
        return False

    logger.warning("Pushover queued request %s", body.get("request"))
    return True
