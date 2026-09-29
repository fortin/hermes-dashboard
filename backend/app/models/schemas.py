import re

from pydantic import BaseModel, Field, field_validator


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
    project_id: str | None = None
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


_WEEKDAY = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTH = (
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_WRITTEN_DATE = (
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH})(?:\s+\d{{4}})?"
    rf"|(?:{_MONTH})\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?)"
)
_DATE_HEADING = re.compile(
    rf"^(?:(?:{_WEEKDAY})(?:\s*,\s*|\s+))?(?:\d{{4}}-\d{{2}}-\d{{2}}|{_WRITTEN_DATE})$"
    rf"|^(?:{_WEEKDAY})$",
    re.IGNORECASE,
)


def strip_leading_date(summary: str) -> str:
    """Drop a date-only first line. The dashboard header already shows the day."""
    lines = (summary or "").split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return (summary or "").strip()
    heading = re.sub(r"^#{1,3}\s*", "", lines[index].strip())
    heading = re.sub(r"^(?:\*\*|__)|(?:\*\*|__)$", "", heading).strip()
    if _DATE_HEADING.fullmatch(heading) is None:
        return (summary or "").strip()
    index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    rest = "\n".join(lines[index:]).strip()
    return rest or (summary or "").strip()


def markdown_paragraphs(summary: str) -> str:
    """Turn single newlines into blank lines so Markdown shows separate paragraphs."""
    text = (summary or "").replace("\r\n", "\n").strip()
    return re.sub(r"(?<!\n)\n(?!\n)", "\n\n", text)


class Briefing(BaseModel):
    summary: str
    generated_at: str
    source: str = "hermes"
    suggested_task_ids: list[str] = Field(default_factory=list)
    horizon: str = "today"
    # Fantastical + OmniFocus snapshot hash; empty on legacy briefings.
    context_fingerprint: str = ""

    @field_validator("summary")
    @classmethod
    def _drop_leading_date(cls, value: str) -> str:
        return markdown_paragraphs(strip_leading_date(value))


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

