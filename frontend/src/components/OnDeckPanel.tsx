import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { api, type TaskItem } from '../api'
import { briefingQueryOptions } from './BriefingPanel'

type UndoState = { task: TaskItem; timer: number } | null

function pinSuggestedTasks(tasks: TaskItem[], suggestedIds: string[]): TaskItem[] {
  if (!suggestedIds.length) return tasks
  const rank = new Map(suggestedIds.map((id, i) => [id, i]))
  const pinned: TaskItem[] = []
  const rest: TaskItem[] = []
  for (const task of tasks) {
    if (rank.has(task.id)) pinned.push(task)
    else rest.push(task)
  }
  pinned.sort((a, b) => (rank.get(a.id) ?? 0) - (rank.get(b.id) ?? 0))
  return [...pinned, ...rest]
}

export function OnDeckPanel() {
  const qc = useQueryClient()
  const [undo, setUndo] = useState<UndoState>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.onDeck(),
    staleTime: 90_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  })

  const briefing = useQuery(briefingQueryOptions)

  const suggestedIds = briefing.data?.suggested_task_ids
  const suggested = useMemo(
    () => new Set(suggestedIds ?? []),
    [suggestedIds],
  )
  const tasks = useMemo(
    () => pinSuggestedTasks(data || [], suggestedIds ?? []),
    [data, suggestedIds],
  )

  async function refresh() {
    setError(null)
    try {
      const tasks = await api.onDeck(true)
      qc.setQueryData(['tasks'], tasks)
      const status = await api.status(true)
      qc.setQueryData(['status'], status)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Refresh failed')
    }
  }

  const complete = useMutation({
    mutationFn: (task: TaskItem) => api.completeTask(task.id),
    onMutate: async (task) => {
      setError(null)
      await qc.cancelQueries({ queryKey: ['tasks'] })
      const prev = qc.getQueryData<TaskItem[]>(['tasks'])
      qc.setQueryData<TaskItem[]>(['tasks'], (old) =>
        (old || []).filter((t) => t.id !== task.id),
      )
      return { prev, task }
    },
    onError: (err, _task, ctx) => {
      if (ctx?.prev) qc.setQueryData(['tasks'], ctx.prev)
      setError(err instanceof Error ? err.message : 'Could not complete task')
    },
    onSuccess: (_res, task) => {
      if (undo?.timer) window.clearTimeout(undo.timer)
      const timer = window.setTimeout(() => setUndo(null), 5000)
      setUndo({ task, timer })
      void qc.invalidateQueries({ queryKey: ['status'] })
      void qc.invalidateQueries({ queryKey: ['briefing'] })
    },
  })

  const incomplete = useMutation({
    mutationFn: (task: TaskItem) => api.incompleteTask(task.id),
    onSuccess: (_res, task) => {
      qc.setQueryData<TaskItem[]>(['tasks'], (old) => [task, ...(old || [])])
      setUndo(null)
      void qc.invalidateQueries({ queryKey: ['tasks'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
      void qc.invalidateQueries({ queryKey: ['briefing'] })
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : 'Undo failed')
    },
  })

  const add = useMutation({
    mutationFn: (name: string) => api.addTask(name),
    onSuccess: () => {
      setDraft('')
      void qc.invalidateQueries({ queryKey: ['tasks'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
      void qc.invalidateQueries({ queryKey: ['briefing'] })
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : 'Could not add task')
    },
  })

  useEffect(() => {
    return () => {
      if (undo?.timer) window.clearTimeout(undo.timer)
    }
  }, [undo])

  return (
    <section className="panel on-deck-panel">
      <header className="panel-head">
        <h2>On Deck</h2>
        <button
          type="button"
          className="text-btn"
          onClick={() => void refresh()}
          disabled={isFetching}
        >
          {isFetching ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>
      <form
        className="add-task"
        onSubmit={(e) => {
          e.preventDefault()
          const name = draft.trim()
          if (name) add.mutate(name)
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add task…"
          aria-label="Add OmniFocus task"
        />
        <button type="submit" disabled={!draft.trim() || add.isPending}>
          +
        </button>
      </form>
      {isLoading && <p className="muted">Loading tasks…</p>}
      {error && <p className="error-line">{error}</p>}
      <ul className="task-list">
        {tasks.map((task) => (
          <li key={task.id}>
            <label className={`task-row${suggested.has(task.id) ? ' suggested' : ''}`}>
              <input
                type="checkbox"
                checked={false}
                onChange={() => complete.mutate(task)}
                aria-label={`Complete ${task.name}`}
              />
              <span className="task-body">
                <span className="task-name">
                  {task.name}
                  {suggested.has(task.id) && (
                    <span className="task-suggest">Next</span>
                  )}
                </span>
                {(task.project || task.due) && (
                  <span className="task-meta">
                    {[task.project, task.due ? `due ${task.due}` : null]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                )}
              </span>
            </label>
          </li>
        ))}
      </ul>
      {!isLoading && data && data.length === 0 && (
        <p className="muted">On Deck is clear.</p>
      )}

      {undo && (
        <div className="undo-bar">
          <span>Completed “{undo.task.name}”</span>
          <button type="button" onClick={() => incomplete.mutate(undo.task)}>
            Undo
          </button>
        </div>
      )}
    </section>
  )
}
