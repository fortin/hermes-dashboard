import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api, type EmailTriageItem } from '../api'
import { briefingQueryOptions } from './BriefingPanel'

function IconSend() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path
        fill="currentColor"
        d="M3.4 20.4 21 12 3.4 3.6 3 10.2 15 12 3 13.8z"
      />
    </svg>
  )
}

function IconCopy() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path
        fill="currentColor"
        d="M8 7h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2zm-3 3H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1H8a4 4 0 0 0-4 4v5z"
      />
    </svg>
  )
}

function IconTrash() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path
        fill="currentColor"
        d="M9 3h6l1 2h4v2H4V5h4l1-2zm1 6h2v9h-2V9zm4 0h2v9h-2V9zM7 9h2v9H7V9z"
      />
    </svg>
  )
}

function EmailRow({ item }: { item: EmailTriageItem }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(item.draft_reply || '')
  const [status, setStatus] = useState<string | null>(null)

  const send = useMutation({
    mutationFn: () =>
      api.sendEmailReply({
        account: item.account,
        message_id: item.id,
        body: draft,
      }),
    onSuccess: () => {
      setStatus('Sent')
      void qc.invalidateQueries({ queryKey: ['email'] })
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : 'Send failed'),
  })

  const remove = useMutation({
    mutationFn: () =>
      api.deleteEmail({
        account: item.account,
        message_id: item.id,
        body: '',
      }),
    onSuccess: () => {
      setStatus('Deleted')
      void qc.invalidateQueries({ queryKey: ['email'] })
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : 'Delete failed'),
  })

  async function copyDraft() {
    try {
      await navigator.clipboard.writeText(draft)
      setStatus('Copied')
    } catch {
      setStatus('Copy failed')
    }
  }

  return (
    <li className={`email-row priority-${item.priority}`}>
      <div className="email-main">
        <div className="email-meta">
          <span className={`priority-pill ${item.priority}`}>{item.priority}</span>
          <span className="muted tiny">{item.account}</span>
          <span className="muted tiny">{item.disposition.replace(/_/g, ' ')}</span>
        </div>
        <div className="email-subject">{item.subject}</div>
        <div className="email-from muted">{item.sender}</div>
        {item.reason && <p className="email-reason muted">{item.reason}</p>}
      </div>
      {item.draft_reply && (
        <div className="email-actions">
          <button
            type="button"
            className={`icon-btn ${open ? 'active' : ''}`}
            aria-label={open ? 'Hide draft' : 'Show draft'}
            title={open ? 'Hide draft' : 'Show draft'}
            onClick={() => setOpen((v) => !v)}
          >
            ✎
          </button>
        </div>
      )}
      {open && item.draft_reply && (
        <div className="email-draft">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Draft reply"
          />
          <div className="draft-actions">
            <button
              type="button"
              className="icon-btn"
              aria-label="Send"
              title="Send"
              disabled={send.isPending || !draft.trim()}
              onClick={() => send.mutate()}
            >
              <IconSend />
            </button>
            <button
              type="button"
              className="icon-btn"
              aria-label="Copy"
              title="Copy"
              onClick={() => void copyDraft()}
            >
              <IconCopy />
            </button>
            <button
              type="button"
              className="icon-btn danger"
              aria-label="Delete"
              title="Delete"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              <IconTrash />
            </button>
            {status && <span className="muted tiny">{status}</span>}
          </div>
        </div>
      )}
    </li>
  )
}

export function EmailPanel() {
  const qc = useQueryClient()
  const briefing = useQuery(briefingQueryOptions)

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['email'],
    queryFn: () => api.emailTriage(),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    // Wait until the first briefing attempt finishes so triage doesn't
    // occupy the single Hermes slot during initial load.
    enabled: briefing.isFetched,
  })

  async function refresh() {
    const triage = await api.emailTriageForce()
    qc.setQueryData(['email'], triage)
  }

  const visible = useMemo(() => {
    const items = data?.items || []
    return items.filter((i) => i.priority !== 'noise')
  }, [data])

  const noiseCount = (data?.items || []).filter((i) => i.priority === 'noise').length

  return (
    <section className="panel email-panel">
      <header className="panel-head">
        <div>
          <h2>Email</h2>
          <p className="muted tiny">Unread &amp; flagged only</p>
          {noiseCount > 0 && (
            <p className="muted tiny">{noiseCount} promotional/noise hidden</p>
          )}
        </div>
        <button type="button" className="text-btn" onClick={() => void refresh()} disabled={isFetching}>
          Refresh
        </button>
      </header>
      {(isLoading || briefing.isLoading) && <p className="muted">Triaging inbox…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      {!isLoading && !briefing.isLoading && visible.length === 0 && (
        <p className="muted">No priority mail right now.</p>
      )}
      <ul className="email-list">
        {visible.map((item) => (
          <EmailRow key={`${item.account}:${item.id}`} item={item} />
        ))}
      </ul>
      {data?.generated_at && (
        <p className="muted tiny">
          {data.source === 'fallback' ? 'Heuristic · ' : ''}
          {data.source === 'apple' ? 'Apple Intelligence · ' : ''}
          {new Date(data.generated_at).toLocaleTimeString()}
        </p>
      )}
    </section>
  )
}
