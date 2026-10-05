from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import AgentAsk, AgentReply, Briefing
from ..services import fantastical, omnifocus
from ..services.briefing_push import briefing_context
from ..services.hermes import (
    HermesUnavailable,
    ask_day,
    generate_briefing,
    local_now_context,
)

router = APIRouter(prefix="/api", tags=["agent"])


@router.post("/agent/ask", response_model=AgentReply)
async def agent_ask(body: AgentAsk) -> AgentReply:
    context = body.context
    if context is None:
        try:
            context = {
                "calendar": [e.model_dump() for e in await fantastical.get_today()],
                "tasks": [t.model_dump() for t in await omnifocus.get_on_deck()],
                "now": local_now_context(),
            }
        except Exception:  # noqa: BLE001
            context = {"now": local_now_context()}
    elif "now" not in context:
        context = {**context, "now": local_now_context()}
    try:
        return await ask_day(body.message, context=context)
    except HermesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/briefing", response_model=Briefing)
@router.post("/briefing", response_model=Briefing)
async def briefing(force: bool = Query(False)) -> Briefing:
    # Always load Fantastical + On Deck. force Refresh also re-reads On Deck
    # so a stale empty cache cannot keep a held tomorrow plan on screen.
    return await generate_briefing(
        await briefing_context(force=force),
        force=force,
    )
