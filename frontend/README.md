# Hermes Dashboard — frontend

Single-page React UI for the local Hermes Dashboard API. The production build is served by FastAPI from `frontend/dist`; during development, Vite runs on port 5173 and proxies `/api` to the backend on `:8787`.

## Stack

- React 19 + TypeScript
- Vite 8
- [TanStack Query](https://tanstack.com/query) for server state
- [react-markdown](https://github.com/remarkjs/react-markdown) + remark-gfm for briefing and daily-note rendering
- Oxlint (`npm run lint`)

## Commands

From this directory:

```bash
npm install
npm run dev      # http://127.0.0.1:5173 — requires API on :8787
npm run build    # typecheck + output to dist/ (used by ./scripts/run.sh)
npm run preview  # preview production build locally
npm run lint
```

From the repo root, `./scripts/build-frontend.sh` runs `npm run build` here.

## Development

1. Start the backend (see root [README](../README.md)): `./scripts/run.sh` or uvicorn on `:8787`.
2. Run `npm run dev` and open [http://127.0.0.1:5173](http://127.0.0.1:5173).

[`vite.config.ts`](vite.config.ts) proxies all `/api/*` requests to `http://127.0.0.1:8787`, so the browser always uses relative `/api/...` URLs (same as production).

## Layout

| Path | Role |
| --- | --- |
| [`src/App.tsx`](src/App.tsx) | Shell: header (date + timezone), three-column grid |
| [`src/api.ts`](src/api.ts) | Typed `fetch` wrapper and API methods |
| [`src/components/HermesBar.tsx`](src/components/HermesBar.tsx) | Ask Hermes input; `useSseRefresh()` listens on `/api/events` |
| [`src/components/TimezoneSelect.tsx`](src/components/TimezoneSelect.tsx) | Header timezone dropdown; auto-detect when API `source` is `auto` |
| [`src/components/BriefingPanel.tsx`](src/components/BriefingPanel.tsx) | Briefing + quick status widgets |
| [`src/components/EmailPanel.tsx`](src/components/EmailPanel.tsx) | Email triage |
| [`src/components/DailyNotePanel.tsx`](src/components/DailyNotePanel.tsx) | Obsidian daily note editor |
| [`src/components/CalendarPanel.tsx`](src/components/CalendarPanel.tsx) | Today’s calendar |
| [`src/components/OnDeckPanel.tsx`](src/components/OnDeckPanel.tsx) | OmniFocus On Deck |
| [`src/components/MarkdownView.tsx`](src/components/MarkdownView.tsx) | Shared markdown renderer |

Styles live in [`src/App.css`](src/App.css) and [`src/index.css`](src/index.css).

## Data fetching

- **Queries** use stable keys such as `['briefing']`, `['calendar']`, `['on-deck']`, `['timezone']`, etc.
- **SSE:** `useSseRefresh` in `HermesBar.tsx` opens `EventSource('/api/events')`. `tick` events invalidate or patch calendar and briefing queries; they do not poll On Deck (by design).
- **Timezone changes:** `TimezoneSelect` calls `PUT /api/preferences/timezone` and invalidates `calendar` and `briefing` so times realign with the new zone.

## Adding an API call

1. Extend types and methods in [`src/api.ts`](src/api.ts).
2. Use `useQuery` / `useMutation` from TanStack Query in the relevant panel component.
3. If the backend adds SSE refresh targets, handle the new id in `useSseRefresh` inside `HermesBar.tsx`.

## Linting

Oxlint is configured in [`.oxlintrc.json`](.oxlintrc.json). For stricter type-aware rules, see the [Oxlint documentation](https://oxc.rs/docs/guide/usage/linter/rules) and optional `oxlint-tsgolint` setup described in upstream Vite templates.
