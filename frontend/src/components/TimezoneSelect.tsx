import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

function browserTimezones(): string[] {
  try {
    const supported = (
      Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    ).supportedValuesOf?.('timeZone')
    if (supported?.length) return supported
  } catch {
    /* ignore */
  }
  return []
}

function detectedTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

export function TimezoneSelect() {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const autoApplied = useRef(false)
  const { data, isLoading } = useQuery({
    queryKey: ['timezone'],
    queryFn: () => api.getTimezone(),
    staleTime: 60_000,
  })

  useEffect(() => {
    if (!data || data.source !== 'auto' || autoApplied.current) return
    const detected = detectedTimezone()
    if (!detected || detected === data.timezone) {
      autoApplied.current = true
      return
    }
    autoApplied.current = true
    setBusy(true)
    void api
      .setTimezone(detected)
      .then((next) => {
        qc.setQueryData(['timezone'], next)
      })
      .catch(() => {
        /* keep auto/env fallback */
      })
      .finally(() => setBusy(false))
  }, [data, qc])

  const options = useMemo(() => {
    const fromBrowser = browserTimezones()
    const fromApi = data?.options ?? []
    const current = data?.timezone
    const detected = detectedTimezone()
    const merged = new Set<string>([...fromBrowser, ...fromApi, detected])
    if (current) merged.add(current)
    return Array.from(merged).sort((a, b) => a.localeCompare(b))
  }, [data])

  async function onChange(value: string) {
    setBusy(true)
    try {
      const next = await api.setTimezone(value)
      qc.setQueryData(['timezone'], next)
      void qc.invalidateQueries({ queryKey: ['calendar'] })
      void qc.invalidateQueries({ queryKey: ['briefing'] })
    } finally {
      setBusy(false)
    }
  }

  if (isLoading && !data) {
    return <span className="timezone-select muted tiny">Timezone…</span>
  }

  return (
    <label className="timezone-select">
      <span className="sr-only">Timezone</span>
      <select
        value={data?.timezone ?? detectedTimezone()}
        onChange={(e) => void onChange(e.target.value)}
        disabled={busy}
        aria-label="Timezone"
      >
        {options.map((tz) => (
          <option key={tz} value={tz}>
            {tz.replace(/_/g, ' ')}
          </option>
        ))}
      </select>
    </label>
  )
}
