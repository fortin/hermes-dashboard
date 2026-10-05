from datetime import datetime

from ..localtime import get_tz
from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import (
    DataviewResolveRequest,
    DataviewResolveResponse,
    EmailDraftAction,
    EmailTriageResponse,
)
from ..services.dataview import resolve_dataview_block_async
from ..services.email_triage import delete_message, send_reply, triage_inbox

router = APIRouter(prefix="/api", tags=["email-dataview"])


@router.post("/dataview/resolve", response_model=DataviewResolveResponse)
async def dataview_resolve(body: DataviewResolveRequest) -> DataviewResolveResponse:
    result = await resolve_dataview_block_async(body.query, body.note_content)
    return DataviewResolveResponse(**result)


@router.get("/email/triage", response_model=EmailTriageResponse)
@router.post("/email/triage", response_model=EmailTriageResponse)
async def get_email_triage(force: bool = Query(False)) -> EmailTriageResponse:
    try:
        result = await triage_inbox(force=force)
        if not result.generated_at:
            result.generated_at = datetime.now(get_tz()).isoformat()
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/email/send")
async def email_send(body: EmailDraftAction) -> dict:
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="Draft body is empty")
    try:
        return await send_reply(body.account, body.message_id, body.body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/email/delete")
async def email_delete(body: EmailDraftAction) -> dict:
    try:
        return await delete_message(body.account, body.message_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
