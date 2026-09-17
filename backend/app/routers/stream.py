"""SSE stream endpoint for real-time dashboard updates.

Pushes widget updates, status changes, and agent responses to the
frontend as they happen, avoiding the need for polling.
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse
from app.widgets import widget_registry

router = APIRouter(tags=["stream"])

# Simple event bus - in production, use Redis or a proper pub/sub
_event_buffer: list[dict] = []
_max_buffer = 100


async def publish_event(event: str, data: dict) -> None:
    """Publish an event to the SSE stream."""
    entry = {
        "event": event,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _event_buffer.append(entry)
    if len(_event_buffer) > _max_buffer:
        _event_buffer.pop(0)


async def event_generator():
    """Yield events from the buffer to connected clients."""
    last_seen = None  # Track the last event the client saw
    while True:
        for event in _event_buffer:
            if last_seen is None or event.get("timestamp") > last_seen:
                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"]),
                }
                last_seen = event["timestamp"]
        await asyncio.sleep(0.5)  # Poll interval


@router.get("/stream")
async def sse_stream():
    """SSE endpoint for real-time dashboard updates."""
    return EventSourceResponse(event_generator())
