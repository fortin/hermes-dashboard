"""User preferences backed by Postgres."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import localtime
from ..db import Preference, get_session

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


class TimezoneBody(BaseModel):
    timezone: str | None = Field(
        default=None,
        description="IANA timezone name, or null/empty to use automatic detection.",
    )


class TimezoneResponse(BaseModel):
    timezone: str
    source: str
    options: list[str]


@router.get("/timezone", response_model=TimezoneResponse)
async def get_timezone() -> TimezoneResponse:
    await localtime.load_timezone_preference()
    return TimezoneResponse(
        timezone=localtime.timezone_name(),
        source=localtime.timezone_source(),
        options=list(localtime.common_timezones()),
    )


@router.put("/timezone", response_model=TimezoneResponse)
async def put_timezone(
    body: TimezoneBody,
    session: AsyncSession = Depends(get_session),
) -> TimezoneResponse:
    raw = (body.timezone or "").strip()
    if raw:
        try:
            ZoneInfo(raw)
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(
                status_code=400, detail=f"Unknown timezone: {raw}"
            ) from exc
        localtime.configure_timezone(raw)
        row = await session.get(Preference, localtime.preference_key())
        if row is None:
            session.add(Preference(key=localtime.preference_key(), value=raw))
        else:
            row.value = raw
    else:
        localtime.configure_timezone("")
        row = await session.get(Preference, localtime.preference_key())
        if row is not None:
            await session.delete(row)
    await session.commit()
    return TimezoneResponse(
        timezone=localtime.timezone_name(),
        source=localtime.timezone_source(),
        options=list(localtime.common_timezones()),
    )
