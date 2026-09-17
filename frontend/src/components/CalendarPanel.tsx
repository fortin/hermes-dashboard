import { useQuery } from '@tanstack/react-query'
import { api, type CalendarEvent } from '../api'

function formatTime(value: string, allDay: boolean): string {
  if (allDay) return 'All day'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) {
    // Fantastical sometimes returns human strings
    return value.slice(0, 16)
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function openInFantastical(event: CalendarEvent) {
  if (event.url) {
    window.location.href = event.url
    return
  }
  // Fall back to Fantastical search URL scheme
  const q = encodeURIComponent(event.title)
  window.location.href = `x-fantastical3://show?search=${q}`
}

export function CalendarPanel() {
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['calendar'],
    queryFn: api.calendarToday,
    staleTime: 60_000,
    refetchInterval: 60_000,
  })

  return (
    <section className="panel calendar-panel">
      <header className="panel-head">
        <h2>Today</h2>
        {isFetching && !isLoading && <span className="muted">Refreshing…</span>}
      </header>
      {isLoading && <p className="muted">Loading calendar…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      <ol className="timeline">
        {(data || []).map((event) => {
          const meta = [event.calendar, event.location].filter(Boolean).join(' · ')
          return (
            <li key={event.id}>
              <button type="button" className="event-row" onClick={() => openInFantastical(event)}>
                <span className="event-time">{formatTime(event.start, event.all_day)}</span>
                <span className="event-body">
                  <span className="event-title">{event.title}</span>
                  {meta && (
                    <span className="event-meta" title={meta}>
                      {meta}
                    </span>
                  )}
                </span>
              </button>
            </li>
          )
        })}
      </ol>
      {!isLoading && data && data.length === 0 && (
        <p className="muted">No events on the calendar today.</p>
      )}
    </section>
  )
}
