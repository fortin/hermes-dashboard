import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import ActionJournal, get_session
from ..models.schemas import StatusCounts, TaskItem
from ..services import omnifocus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class AddTaskBody(BaseModel):
    name: str = Field(min_length=1)
    note: str | None = None


@router.get("/on-deck", response_model=list[TaskItem])
async def on_deck(force: bool = Query(False)) -> list[TaskItem]:
    try:
        return await omnifocus.get_on_deck(force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/status", response_model=StatusCounts)
async def status(force: bool = Query(False)) -> StatusCounts:
    try:
        return await omnifocus.get_status_counts(force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _journal(
    session: AsyncSession,
    *,
    action: str,
    target_id: str,
    status: str,
    error: str | None = None,
) -> int | None:
    """Best-effort action journal — never blocks OmniFocus mutations."""
    try:
        row = ActionJournal(
            action=action,
            target_id=target_id,
            payload=json.dumps({}),
            status=status,
            error=error,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception("action journal write failed (%s)", action)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


@router.post("/{task_id}/complete")
async def complete(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        result = await omnifocus.complete_task(task_id)
    except Exception as exc:  # noqa: BLE001
        await _journal(
            session,
            action="complete_task",
            target_id=task_id,
            status="error",
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    journal_id = await _journal(
        session,
        action="complete_task",
        target_id=task_id,
        status="ok",
    )
    return {"ok": True, "result": result, "journal_id": journal_id}


@router.post("/{task_id}/incomplete")
async def incomplete(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        result = await omnifocus.incomplete_task(task_id)
    except Exception as exc:  # noqa: BLE001
        await _journal(
            session,
            action="incomplete_task",
            target_id=task_id,
            status="error",
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    journal_id = await _journal(
        session,
        action="incomplete_task",
        target_id=task_id,
        status="ok",
    )
    return {"ok": True, "result": result, "journal_id": journal_id}


@router.post("")
async def add_task(body: AddTaskBody) -> dict:
    try:
        result = await omnifocus.add_task(body.name, body.note)
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
