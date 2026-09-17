from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..models.schemas import DailyNote, DailyNoteUpdate
from ..services import obsidian
from ..services.obsidian import ConflictError

router = APIRouter(prefix="/api/note", tags=["note"])


@router.get("/today", response_model=DailyNote)
async def today_note() -> DailyNote:
    try:
        return await obsidian.get_today_note(create=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.patch("/today")
async def patch_today(body: DailyNoteUpdate) -> Any:
    try:
        return await obsidian.update_today_note(body.content, body.revision)
    except ConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "conflict",
                "revision": exc.revision,
                "content": exc.content,
            },
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
