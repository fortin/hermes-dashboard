import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api, type DailyNote } from '../api'
import { MarkdownView } from './MarkdownView'

export function DailyNotePanel() {
  const qc = useQueryClient()
  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ['note'],
    queryFn: api.todayNote,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  })
  const [content, setContent] = useState('')
  const [revision, setRevision] = useState('')
  const [baseline, setBaseline] = useState('')
  const [editing, setEditing] = useState(false)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'conflict' | 'error'>(
    'idle',
  )
  const [message, setMessage] = useState<string | null>(null)

  const contentRef = useRef(content)
  const revisionRef = useRef(revision)
  const baselineRef = useRef(baseline)
  const savingRef = useRef(false)
  const pendingRef = useRef(false)
  const timerRef = useRef<number | null>(null)
  const seeded = useRef(false)

  contentRef.current = content
  revisionRef.current = revision
  baselineRef.current = baseline

  useEffect(() => {
    if (data && !seeded.current) {
      setContent(data.content)
      setRevision(data.revision)
      setBaseline(data.content)
      seeded.current = true
      if (data.created) {
        setMessage('Created today’s note from your Daily Template.')
      }
    }
  }, [data])

  async function flushSave() {
    if (savingRef.current) {
      pendingRef.current = true
      return
    }

    const toSave = contentRef.current
    const rev = revisionRef.current
    if (toSave === baselineRef.current) {
      return
    }

    savingRef.current = true
    setSaveState('saving')
    setMessage(null)

    try {
      const note = await api.saveNote(toSave, rev)
      revisionRef.current = note.revision
      setRevision(note.revision)

      if (contentRef.current === toSave) {
        contentRef.current = note.content
        baselineRef.current = note.content
        setContent(note.content)
        setBaseline(note.content)
        qc.setQueryData(['note'], note)
        setSaveState('saved')
      } else {
        // User kept typing — accept the new revision and save again.
        baselineRef.current = note.content
        setBaseline(note.content)
        qc.setQueryData(['note'], {
          ...note,
          content: contentRef.current,
          revision: note.revision,
        } satisfies DailyNote)
        pendingRef.current = true
        setSaveState('saving')
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const body = err.body as { revision?: string; content?: string } | null
        setSaveState('conflict')
        setMessage('Edited in Obsidian — reload to avoid overwriting.')
        if (body?.revision && body.content) {
          // Keep local draft, but refresh the server revision metadata for a clean reload.
          qc.setQueryData(['note'], {
            path: data?.path || '',
            date: data?.date || '',
            content: body.content,
            revision: body.revision,
            created: false,
            sections: data?.sections || [],
          } satisfies DailyNote)
        }
      } else {
        setSaveState('error')
        setMessage(err instanceof Error ? err.message : 'Save failed')
      }
    } finally {
      savingRef.current = false
      if (pendingRef.current) {
        pendingRef.current = false
        void flushSave()
      }
    }
  }

  function scheduleSave() {
    if (timerRef.current) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => {
      void flushSave()
    }, 800)
  }

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current)
    }
  }, [])

  function applyNote(note: DailyNote) {
    contentRef.current = note.content
    revisionRef.current = note.revision
    baselineRef.current = note.content
    setContent(note.content)
    setRevision(note.revision)
    setBaseline(note.content)
    seeded.current = true
  }

  async function reloadFromVault() {
    if (timerRef.current) window.clearTimeout(timerRef.current)
    pendingRef.current = false
    setSaveState('idle')
    setMessage(null)
    setEditing(false)
    const result = await refetch()
    if (!result.isSuccess || !result.data) return
    applyNote(result.data)
    if (result.data.created) {
      setMessage('Created today’s note from your Daily Template.')
    }
  }

  async function refreshNote() {
    if (savingRef.current) return
    if (contentRef.current !== baselineRef.current) {
      const discard = window.confirm('Discard unsaved changes and reload the daily note?')
      if (!discard) return
    }
    await reloadFromVault()
  }

  async function toggleEditing() {
    if (editing) {
      if (timerRef.current) window.clearTimeout(timerRef.current)
      await flushSave()
      setEditing(false)
    } else {
      setEditing(true)
    }
  }

  const filename = data?.path?.split('/').pop()
  const dirty = content !== baseline

  return (
    <section className="panel note-panel">
      <header className="panel-head">
        <div>
          <h2>Daily Note</h2>
          {data?.date && (
            <p className="muted tiny">
              {filename} · {data.date}
              {data.created ? ' · new from template' : ''}
              {dirty && editing ? ' · unsaved' : ''}
            </p>
          )}
        </div>
        <div className="panel-actions">
          <span className="muted">
            {saveState === 'saving' && 'Saving…'}
            {saveState === 'saved' && 'Saved ✓'}
            {saveState === 'conflict' && 'Conflict'}
            {saveState === 'error' && 'Save failed'}
          </span>
          <button
            type="button"
            className="text-btn"
            onClick={() => void refreshNote()}
            disabled={isLoading || isFetching || saveState === 'saving' || !data}
          >
            {isFetching && !isLoading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            type="button"
            className="text-btn"
            onClick={() => void toggleEditing()}
            disabled={isLoading || !content}
          >
            {editing ? 'Preview' : 'Edit'}
          </button>
        </div>
      </header>
      {isLoading && <p className="muted">Loading note…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      {message && <p className={saveState === 'conflict' ? 'error-line' : 'muted'}>{message}</p>}
      {saveState === 'conflict' && (
        <button type="button" className="text-btn" onClick={() => void reloadFromVault()}>
          Reload from Obsidian
        </button>
      )}
      {!isLoading && content && (
        editing ? (
          <textarea
            className="note-editor"
            value={content}
            onChange={(e) => {
              setContent(e.target.value)
              if (saveState !== 'saving') setSaveState('idle')
              scheduleSave()
            }}
            onBlur={() => {
              if (timerRef.current) window.clearTimeout(timerRef.current)
              void flushSave()
            }}
            spellCheck
            aria-label="Daily note"
          />
        ) : (
          <MarkdownView source={content} className="note-preview" hideFrontmatter />
        )
      )}
    </section>
  )
}
