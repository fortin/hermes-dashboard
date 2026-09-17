import { useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { api, type Briefing } from '../api'
import { MarkdownView } from './MarkdownView'

export const briefingQueryOptions = {
  queryKey: ['briefing'] as const,
  queryFn: () => api.briefing(),
  staleTime: (query: Query<Briefing>) =>
    query.state.data?.source === 'hermes' ? 5 * 60_000 : 0,
  refetchInterval: (query: Query<Briefing>) =>
    query.state.data?.source === 'fallback' ? 15_000 : false,
  refetchOnWindowFocus: (query: Query<Briefing>) =>
    query.state.data?.source === 'fallback',
}

export function BriefingPanel() {
  const qc = useQueryClient()
  const { data, isLoading, error, isFetching } = useQuery(briefingQueryOptions)

  async function refresh() {
    const briefing = await api.briefingForce()
    qc.setQueryData(['briefing'], briefing)
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Hermes Briefing</h2>
        <button type="button" className="text-btn" onClick={() => void refresh()} disabled={isFetching}>
          Refresh
        </button>
      </header>
      {isLoading && <p className="muted">Generating briefing…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      {data && (
        <>
          {isFetching && !isLoading && <p className="muted">Still talking to Hermes…</p>}
          <MarkdownView source={data.summary} className="briefing-body" />
          <p className="muted tiny">
            {data.source === 'fallback' ? 'Offline fallback · ' : ''}
            {data.horizon === 'tomorrow' ? 'Tomorrow · ' : ''}
            {new Date(data.generated_at).toLocaleTimeString()}
          </p>
        </>
      )}
    </section>
  )
}

export function StatusPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['status'],
    queryFn: () => api.status(),
    staleTime: 90_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  })

  const rows: [string, number | null | undefined][] = [
    ['OF Inbox', data?.inbox],
    ['Overdue', data?.overdue],
    ['Flagged', data?.flagged],
    ['On Deck', data?.on_deck],
  ]

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Quick Status</h2>
      </header>
      {isLoading && <p className="muted">Loading…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      <dl className="status-grid">
        {rows.map(([label, value]) => (
          <div key={label} className="status-row">
            <dt>{label}</dt>
            <dd>{value == null ? '—' : value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
