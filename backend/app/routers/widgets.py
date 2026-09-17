import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..services.hermes import briefing_generation, peek_briefing
from ..widgets.registry import list_widgets, load_widget

router = APIRouter(prefix="/api", tags=["widgets"])


@router.get("/widgets")
async def widgets() -> list[dict[str, Any]]:
    return [w.model_dump() for w in list_widgets()]


@router.get("/widgets/{widget_id}")
async def widget_data(widget_id: str) -> Any:
    try:
        return await load_widget(widget_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown widget") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/events")
async def sse_events(request: Request) -> EventSourceResponse:
    """Lightweight heartbeat + refresh hints for the SPA."""

    async def event_generator():
        tick = 0
        last_briefing_gen = briefing_generation()
        while True:
            if await request.is_disconnected():
                break
            tick += 1
            payload = {"tick": tick, "refresh": []}
            # Do not poll OmniFocus on a timer — OmniJS automation freezes OF
            # when Hermes + dashboard both hit MCP. Tasks refresh on mutation
            # or explicit Refresh; calendar is lighter.
            if tick % 120 == 0:
                payload["refresh"].append("calendar_today")
            # Only hint Safari when a valid Hermes briefing was cached.
            # Timer invalidation stacked llama runs; generation is bump-only.
            current_gen = briefing_generation()
            if current_gen != last_briefing_gen:
                last_briefing_gen = current_gen
                payload["refresh"].append("hermes_briefing")
                snapshot = peek_briefing()
                if snapshot is not None:
                    payload["briefing"] = snapshot.model_dump()
            yield {
                "event": "tick",
                "data": json.dumps(payload),
            }
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())
