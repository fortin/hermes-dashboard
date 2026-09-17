"""Hermes Dashboard widgets system."""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence


@dataclass
class Widget:
    """Declares a dashboard card backed by an MCP source."""
    id: str
    title: str
    icon: str
    refresh_interval: int  # seconds between auto-refreshes
    loader: Callable[[], Awaitable[Any]]  # async function returning data
    actions: Sequence[Callable] = field(default_factory=list)
    initial_data: Any = None
    sse_event: str = "widget_update"


class WidgetRegistry:
    """Central registry for dashboard widgets."""

    def __init__(self) -> None:
        self.widgets: list[Widget] = []

    def register(self, widget: Widget) -> Widget:
        """Register a new widget. Returns the widget for chaining."""
        self.widgets.append(widget)
        return widget

    def get(self, widget_id: str) -> Widget | None:
        """Get a widget by ID."""
        for w in self.widgets:
            if w.id == widget_id:
                return w
        return None

    def all(self) -> list[Widget]:
        """Return all registered widgets."""
        return list(self.widgets)


# Global registry instance
widget_registry = WidgetRegistry()
