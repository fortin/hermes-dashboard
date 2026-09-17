from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..models.schemas import WidgetMeta
from ..services import fantastical, omnifocus, obsidian
from ..services.hermes import generate_briefing


Loader = Callable[[], Awaitable[Any]]


@dataclass
class Widget:
    id: str
    title: str
    refresh_interval: int
    loader: Loader
    actions: list[str] = field(default_factory=list)

    def meta(self) -> WidgetMeta:
        return WidgetMeta(
            id=self.id,
            title=self.title,
            refresh_interval=self.refresh_interval,
            actions=list(self.actions),
        )


async def _calendar_loader():
    return [e.model_dump() for e in await fantastical.get_today()]


async def _on_deck_loader():
    return [t.model_dump() for t in await omnifocus.get_on_deck()]


async def _daily_note_loader():
    return (await obsidian.get_today_note()).model_dump()


async def _status_loader():
    return (await omnifocus.get_status_counts()).model_dump()


async def _briefing_loader():
    calendar = await _calendar_loader()
    tasks = await _on_deck_loader()
    note = await _daily_note_loader()
    from ..services.hermes import local_now_context

    briefing = await generate_briefing(
        {
            "calendar": calendar,
            "tasks": tasks,
            "note_path": note.get("path"),
            "now": local_now_context(),
        }
    )
    return briefing.model_dump()


WIDGETS: dict[str, Widget] = {
    "calendar_today": Widget(
        id="calendar_today",
        title="Today",
        refresh_interval=60,
        loader=_calendar_loader,
    ),
    "omnifocus_on_deck": Widget(
        id="omnifocus_on_deck",
        title="On Deck",
        refresh_interval=30,
        loader=_on_deck_loader,
        actions=["complete_task", "undo_complete", "add_task"],
    ),
    "obsidian_daily_note": Widget(
        id="obsidian_daily_note",
        title="Daily Note",
        refresh_interval=0,
        loader=_daily_note_loader,
        actions=["save"],
    ),
    "hermes_briefing": Widget(
        id="hermes_briefing",
        title="Hermes Briefing",
        refresh_interval=300,
        loader=_briefing_loader,
        actions=["refresh"],
    ),
    "quick_status": Widget(
        id="quick_status",
        title="Quick Status",
        refresh_interval=60,
        loader=_status_loader,
    ),
}


def list_widgets() -> list[WidgetMeta]:
    return [w.meta() for w in WIDGETS.values()]


async def load_widget(widget_id: str) -> Any:
    widget = WIDGETS.get(widget_id)
    if not widget:
        raise KeyError(widget_id)
    return await widget.loader()
