import { useQuery } from '@tanstack/react-query'
import { Children, isValidElement } from 'react'
import type { Components } from 'react-markdown'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type DataviewResolveResponse } from '../api'

/** Soften Obsidian wiki-links for HTML preview. */
export function prepareMarkdown(source: string): string {
  return source
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '[$2](#)')
    .replace(/\[\[([^\]]+)\]\]/g, '[$1](#)')
}

export function splitFrontmatter(source: string): { frontmatter: string | null; body: string } {
  if (!source.startsWith('---\n')) return { frontmatter: null, body: source }
  const end = source.indexOf('\n---\n', 4)
  if (end === -1) return { frontmatter: null, body: source }
  return {
    frontmatter: source.slice(4, end),
    body: source.slice(end + 5),
  }
}

function DataviewBlock({
  query,
  noteContent,
}: {
  query: string
  noteContent: string
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['dataview', query, noteContent.slice(0, 80)],
    queryFn: () => api.resolveDataview(query, noteContent),
    staleTime: 60_000,
  })

  return (
    <div className="dataview-card">
      <div className="dataview-card-head">
        <span className="dataview-badge">{data?.kind || 'dataview'}</span>
        <strong>{data?.title || 'Dataview'}</strong>
        {data?.approximate && <span className="muted tiny">approx.</span>}
      </div>
      {isLoading && <p className="muted">Resolving…</p>}
      {error && <p className="error-line">{(error as Error).message}</p>}
      {data && !isLoading && <DataviewResult data={data} />}
    </div>
  )
}

function DataviewResult({ data }: { data: DataviewResolveResponse }) {
  if (data.items.length === 0) {
    return (
      <p className="muted">
        {data.empty}
        {data.query_preview ? ` (${data.query_preview})` : ''}
      </p>
    )
  }
  return (
    <ul className="dataview-list">
      {data.items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  )
}

export function MarkdownView({
  source,
  className,
  hideFrontmatter = false,
}: {
  source: string
  className?: string
  hideFrontmatter?: boolean
}) {
  let text = source
  if (hideFrontmatter) {
    text = splitFrontmatter(source).body
  }
  text = prepareMarkdown(text)

  const components: Components = {
    pre({ children }) {
      const child = Children.toArray(children)[0]
      if (isValidElement(child)) {
        const codeClass = String(
          (child.props as { className?: string }).className || '',
        )
        const lang = /language-([\w-]+)/.exec(codeClass)?.[1]
        const raw = String((child.props as { children?: unknown }).children || '').replace(
          /\n$/,
          '',
        )
        if (lang === 'dataview' || lang === 'dataviewjs') {
          return <DataviewBlock query={raw} noteContent={source} />
        }
      }
      return <pre>{children}</pre>
    },
  }

  return (
    <div className={className ? `markdown ${className}` : 'markdown'}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
