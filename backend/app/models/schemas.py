from pydantic import BaseModel, Field


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str
    calendar: str = ""
    location: str | None = None
    all_day: bool = False
    notes: str | None = None
    url: str | None = None


class TaskItem(BaseModel):
    id: str
    name: str
    completed: bool = False
    flagged: bool = False
    project: str | None = None
    due: str | None = None
    defer: str | None = None
    planned: str | None = None
    estimated_minutes: int | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)


class DailyNoteSection(BaseModel):
    heading: str
    content: str


class DailyNote(BaseModel):
    path: str
    date: str
    content: str
    revision: str
    created: bool = False
    sections: list[DailyNoteSection] = Field(default_factory=list)


class DailyNoteUpdate(BaseModel):
    content: str
    revision: str


class AgentAsk(BaseModel):
    message: str
    context: dict[str, object] | None = None


class AgentReply(BaseModel):
    reply: str
    model: str = "hermes-agent"


class Briefing(BaseModel):
    summary: str
    generated_at: str
    source: str = "hermes"
    suggested_task_ids: list[str] = Field(default_factory=list)
    horizon: str = "today"


class StatusCounts(BaseModel):
    inbox: int | None = None
    overdue: int | None = None
    flagged: int | None = None
    on_deck: int | None = None


class WidgetMeta(BaseModel):
    id: str
    title: str
    refresh_interval: int
    actions: list[str] = Field(default_factory=list)


class EmailTriageItem(BaseModel):
    id: str
    account: str
    subject: str
    sender: str
    date: str = ""
    priority: str = "medium"
    disposition: str = "reference"
    reason: str = ""
    needs_reply: bool = False
    draft_reply: str | None = None
    snippet: str = ""


class EmailTriageResponse(BaseModel):
    items: list[EmailTriageItem] = Field(default_factory=list)
    source: str = "hermes"
    generated_at: str = ""


class EmailDraftAction(BaseModel):
    account: str
    message_id: str
    body: str = ""


class DataviewResolveRequest(BaseModel):
    query: str
    note_content: str = ""


class DataviewResolveResponse(BaseModel):
    kind: str = "dataview"
    title: str
    items: list[str] = Field(default_factory=list)
    empty: str = ""
    query_preview: str | None = None
    approximate: bool = True

