export interface CalendarEvent {
  id: string
  title: string
  start: string
  end: string
  calendar: string
  location?: string | null
  all_day: boolean
  notes?: string | null
  url?: string | null
}

export interface TaskItem {
  id: string
  name: string
  completed: boolean
  flagged: boolean
  project?: string | null
  due?: string | null
  defer?: string | null
  estimated_minutes?: number | null
  note?: string | null
  tags: string[]
}

export interface DailyNote {
  path: string
  date: string
  content: string
  revision: string
  created: boolean
  sections: { heading: string; content: string }[]
}

export interface StatusCounts {
  inbox: number | null
  overdue: number | null
  flagged: number | null
  on_deck: number | null
}

export interface Briefing {
  summary: string
  generated_at: string
  source: string
  suggested_task_ids?: string[]
  horizon?: string
}

export interface AgentReply {
  reply: string
  model: string
}

export interface EmailTriageItem {
  id: string
  account: string
  subject: string
  sender: string
  date: string
  priority: string
  disposition: string
  reason: string
  needs_reply: boolean
  draft_reply: string | null
  snippet: string
}

export interface EmailTriageResponse {
  items: EmailTriageItem[]
  source: string
  generated_at: string
}

export interface DataviewResolveResponse {
  kind: string
  title: string
  items: string[]
  empty: string
  query_preview?: string | null
  approximate: boolean
}

export interface TimezonePreference {
  timezone: string
  source: string
  options: string[]
}

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(message: string, status: number, body: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  })
  if (!res.ok) {
    let body: unknown = null
    let detail = res.statusText
    try {
      body = await res.json()
      if (body && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail
        detail = typeof d === 'string' ? d : JSON.stringify(d)
      } else {
        detail = JSON.stringify(body)
      }
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status, body)
  }
  return res.json() as Promise<T>
}

export const api = {
  calendarToday: () => request<CalendarEvent[]>('/api/calendar/today'),
  onDeck: (force = false) =>
    request<TaskItem[]>(`/api/tasks/on-deck${force ? '?force=true' : ''}`),
  status: (force = false) =>
    request<StatusCounts>(`/api/tasks/status${force ? '?force=true' : ''}`),
  completeTask: (id: string) =>
    request<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}/complete`, {
      method: 'POST',
    }),
  incompleteTask: (id: string) =>
    request<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}/incomplete`, {
      method: 'POST',
    }),
  addTask: (name: string) =>
    request<{ ok: boolean }>('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  todayNote: () => request<DailyNote>('/api/note/today'),
  saveNote: (content: string, revision: string) =>
    request<DailyNote>('/api/note/today', {
      method: 'PATCH',
      body: JSON.stringify({ content, revision }),
    }),
  briefing: () => request<Briefing>('/api/briefing', { method: 'POST' }),
  briefingForce: () =>
    request<Briefing>('/api/briefing?force=true', { method: 'POST' }),
  askHermes: (message: string) =>
    request<AgentReply>('/api/agent/ask', {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
  emailTriage: () => request<EmailTriageResponse>('/api/email/triage', { method: 'POST' }),
  emailTriageForce: () =>
    request<EmailTriageResponse>('/api/email/triage?force=true', { method: 'POST' }),
  sendEmailReply: (body: { account: string; message_id: string; body: string }) =>
    request<{ ok: boolean }>('/api/email/send', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  deleteEmail: (body: { account: string; message_id: string; body?: string }) =>
    request<{ ok: boolean }>('/api/email/delete', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  resolveDataview: (query: string, note_content: string) =>
    request<DataviewResolveResponse>('/api/dataview/resolve', {
      method: 'POST',
      body: JSON.stringify({ query, note_content }),
    }),
  getTimezone: () => request<TimezonePreference>('/api/preferences/timezone'),
  setTimezone: (timezone: string | null) =>
    request<TimezonePreference>('/api/preferences/timezone', {
      method: 'PUT',
      body: JSON.stringify({ timezone }),
    }),
}
