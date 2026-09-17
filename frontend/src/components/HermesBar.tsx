import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type Briefing } from '../api'

export function HermesBar() {
  const [value, setValue] = useState('')
  const [reply, setReply] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const qc = useQueryClient()

  async function submit(e?: React.FormEvent) {
    e?.preventDefault()
    const message = value.trim()
    if (!message || busy) return
    setBusy(true)
    setError(null)
    setReply(null)
    try {
      const res = await api.askHermes(message)
      setReply(res.reply)
      setValue('')
      // Agent may have touched calendar/email; OF only if user asks Refresh
      void qc.invalidateQueries({ queryKey: ['calendar'] })
      void qc.invalidateQueries({ queryKey: ['email'] })
      void qc.invalidateQueries({ queryKey: ['briefing'] })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Hermes request failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="hermes-bar">
      <form onSubmit={submit} className="hermes-form">
        <span className="hermes-glyph" aria-hidden>
          ✦
        </span>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Ask Hermes anything about my day…"
          disabled={busy}
          aria-label="Ask Hermes"
        />
        <button type="submit" disabled={busy || !value.trim()}>
          {busy ? 'Thinking…' : 'Ask'}
        </button>
      </form>
      {error && <p className="error-line">{error}</p>}
      {reply && (
        <div className="hermes-reply">
          <pre>{reply}</pre>
        </div>
      )}
    </section>
  )
}

export function useSseRefresh() {
  const qc = useQueryClient()
  useEffect(() => {
    const es = new EventSource('/api/events')
    es.addEventListener('tick', (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data) as {
          refresh?: string[]
          briefing?: Briefing
        }
        for (const id of data.refresh || []) {
          if (id === 'calendar_today') void qc.invalidateQueries({ queryKey: ['calendar'] })
          if (id === 'hermes_briefing') {
            if (data.briefing) qc.setQueryData(['briefing'], data.briefing)
            else void qc.invalidateQueries({ queryKey: ['briefing'] })
          }
          // OmniFocus: no timed SSE refresh (OmniJS contention with Hermes)
        }
      } catch {
        /* ignore */
      }
    })
    return () => es.close()
  }, [qc])

  useEffect(() => {
    const onFocus = () => {
      // Refresh calendar/email/briefing on focus; leave OF to manual Refresh
      // or mutations so window focus doesn't hammer OmniJS.
      void qc.invalidateQueries({ queryKey: ['calendar'] })
      void qc.invalidateQueries({ queryKey: ['email'] })
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [qc])
}
