from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import CalendarEvent
from ..services import fantastical

router = APIRouter(prefix="/api/calendar", tags=["calendar"])
TZ = ZoneInfo("Asia/Bangkok")


@router.get("/today", response_model=list[CalendarEvent])
async def calendar_today() -> list[CalendarEvent]:
    try:
        return await fantastical.get_today()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("", response_model=list[CalendarEvent])
async def calendar_range(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> list[CalendarEvent]:
    try:
        start = (
            datetime.fromisoformat(from_).astimezone(TZ)
            if from_
            else datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        end = (
            datetime.fromisoformat(to).astimezone(TZ)
            if to
            else start + timedelta(days=1)
        )
        return await fantastical.get_events(start, end)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
