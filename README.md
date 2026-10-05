# Hermes Dashboard

A local macOS command center for the day: calendar, OmniFocus, Obsidian, email, and a Hermes agent in one browser page.

The FastAPI backend binds to localhost, talks to your Mac apps over MCP (and Himalaya for mail), and serves a React UI. After a frontend build, the same process hosts the SPA at [http://127.0.0.1:8787](http://127.0.0.1:8787).

## What it does

The dashboard is a single-page layout, not a multi-route app.

| Panel | What you get |
| --- | --- |
| **Ask Hermes** | Free-form questions about the day. The request is sent with today’s calendar, On Deck tasks, and local time. |
| **Hermes Briefing** | A generated summary of the day (cached ~5 minutes). After 17:00 local, the panel switches to a next-day plan when one exists. |
| **Email** | Unread and flagged mail from Himalaya accounts (`gmail`, `icloud`, `zoho`). Hermes triages priority and drafts replies; you can send, copy, or delete. |
| **Daily note** | Today’s Obsidian note, created from your Daily Template if missing. Autosave with conflict detection; Dataview-style blocks are resolved approximately. |
| **Quick Status** | OmniFocus inbox / overdue / flagged / On Deck counts. |
| **Today** | Fantastical events for the current day. Click an event to open it in Fantastical. |
| **On Deck** | The OmniFocus “On Deck” perspective. Complete with a 5-second undo, add a task, and pin items Hermes suggested in the briefing. |
| **Timezone** | Muted header dropdown under the date. On first visit (when no preference is stored), the UI detects the browser IANA zone and saves it. Override anytime; calendar, briefings, and “is the day done?” logic follow the active zone. |

### Timezone resolution

Precedence: **Postgres preference** (set from the UI) → **`DASHBOARD_TIMEZONE` in `.env`** → **host IANA** → **UTC**. Leave `DASHBOARD_TIMEZONE` empty if you want browser auto-detect on first load.

### Apple Intelligence (optional)

When `APPLE_INTELLIGENCE=true`, briefing generation, Ask Hermes, and email triage prefer the macOS `siri` CLI (`SIRI_BIN`, then `~/.local/bin/siri`) with shortcut `SIRI_SHORTCUT`. Hermes on the gateway is the fallback if the shortcut fails or is disabled.

### Briefing publish (optional)

While the API is running, a background loop:

1. Generates a briefing in morning / afternoon / evening slots (not before 07:00, not at night).
2. Writes the markdown summary to `BRIEFING_NOTE_PATH` when that path is set.
3. Sends a [Pushover](https://pushover.net/api) notification when both API keys are set, skipping pushes that are substantively the same as the last one.

After 17:00 the loop will generate a next-day briefing if none is cached, then idle.

### Gladys OmniFocus runner

Tag an OmniFocus task `🤖 Gladys` (the name is the instruction; the note is context) and leave it. Every 15 minutes the dashboard picks **one** eligible task, sends it to Hermes as background work, and writes the receipt back onto that same task.

Eligibility:

- planned time in the past or exactly now: eligible
- planned time later: waits
- no planned date: eligible now

Lifecycle tags (created on first run if missing): `🤖 Gladys`, `gladys-running`, `gladys-done`, `gladys-blocked`, `gladys-failed`. Done tasks are completed. A receipt note is written under `📁 511 🔍 Reviews/Gladys`, and OmniFocus gets a `Review: …` action tagged only `🔎 Review` (so inherited project tags like `(Waiting)` don’t hide it from On Deck), deferred to now, with an Advanced URI to that note. Blocked tasks are flagged so they show up in review. Failed tasks stay incomplete.

This is for agentic work you can leave running — the same class of job as GladysBot on Telegram, not a replacement for Ask Hermes. One Hermes run at a time: if Ask Hermes or a briefing is in flight, the runner skips that cycle. Gladys can schedule follow-up or recurring work with Hermes's `cronjob` tool (results deliver to Telegram).

Disable with `OMNIFOCUS_AGENT_ENABLED=false`.

## Architecture

```
Browser  ──►  FastAPI (:8787)  ──►  Hermes OpenAI-compatible API
                 │                    (local gateway, default :8642)
                 ├── PostgreSQL     cache, preferences, action journal
                 ├── Fantastical    MCP (stdio)
                 ├── OmniFocus      MCP (stdio)
                 ├── Obsidian       MCP (streamable HTTP)
                 ├── Himalaya CLI   email list / send / delete
                 └── Pushover       optional briefing push
```

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy + asyncpg
- **Frontend:** React 19, Vite 8, TanStack Query; markdown via `react-markdown`
- **Live updates:** `/api/events` SSE heartbeats refresh calendar about every two minutes and push a new briefing when one is generated. On Deck is **not** polled on a timer (OmniJS automation contends with Hermes). A separate loop checks the Gladys OmniFocus tag every 15 minutes.

Production serving: if `frontend/dist` exists, FastAPI mounts `/assets` and falls back to `index.html` for the SPA.

## Prerequisites

This is a **local Mac** app. You need:

| Dependency | Why |
| --- | --- |
| macOS | Fantastical MCP helper, OmniFocus, launchd |
| Python 3.11+ | Backend |
| Node.js + npm | Frontend build / Vite |
| PostgreSQL | `hermesdashboard` database |
| [Hermes](https://github.com/NousResearch/hermes-agent) gateway | Briefing + ask + email triage (`API_SERVER_ENABLED=true`) |
| Fantastical | Built-in MCP binary (path in `.env`) |
| OmniFocus + an OmniFocus MCP server | On Deck, status, complete/add |
| Obsidian Local REST / MCP | Daily notes |
| [Himalaya](https://github.com/pimalaya/himalaya) on `PATH` | Email triage (optional if you skip that panel) |

Create the database once:

```bash
createdb hermesdashboard
```

Adjust `DATABASE_URL` if your Postgres role is not the default in `.env.example`.

## Setup

```bash
git clone <this-repo>
cd hermes-dashboard
cp .env.example .env
```

Edit `.env` so every path, token, and URL matches **this machine**. Copy `hermes-rules.local.example` to `hermes-rules.local` if you want custom clock / calendar prompt rules (gitignored).

### Backend

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
```

`scripts/run.sh` creates this venv and installs requirements if they are missing.

Dev tests:

```bash
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend pytest backend/tests
```

### Frontend

```bash
cd frontend
npm install
npm run build    # writes frontend/dist for the FastAPI static server
```

Or use `scripts/build-frontend.sh` from the repo root. See [`frontend/README.md`](frontend/README.md) for UI development (Vite proxy, components, TanStack Query).

## Run

**API + built UI** (what launchd uses):

```bash
./scripts/run.sh
```

Then open [http://127.0.0.1:8787](http://127.0.0.1:8787). Health check: `GET /api/health`.

**Frontend hot reload** while the API is already on `:8787`:

```bash
cd frontend
npm run dev
```

Vite listens on [http://127.0.0.1:5173](http://127.0.0.1:5173) and proxies `/api` to the backend.

### Start at login (launchd)

The plist in `launchd/com.fortin.hermes-dashboard.plist` runs `scripts/run.sh` with `KeepAlive` and writes logs to `~/Library/Logs/hermes-dashboard.{out,err}.log`.

Paths inside the plist are absolute for this checkout. After you confirm they are correct:

```bash
cp launchd/com.fortin.hermes-dashboard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fortin.hermes-dashboard.plist
```

Unload with `launchctl unload ~/Library/LaunchAgents/com.fortin.hermes-dashboard.plist`.

## Environment

Loaded from the repo-root `.env` via pydantic-settings. The dashboard binds **localhost only** by default.

| Variable | Purpose |
| --- | --- |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Bind address (default `127.0.0.1:8787`) |
| `DATABASE_URL` | SQLAlchemy async URL (`postgresql+asyncpg://…`); set in `.env` |
| `OWNER_NAME` | Name used in Hermes / Apple Intelligence prompts (default `you`) |
| `AGENT_NAME` | Background agent name in delegated-task prompts (default `Gladys`) |
| `DASHBOARD_TIMEZONE` | Optional IANA fallback when no UI preference is stored |
| `HERMES_RULES_PATH` | Optional local rules file (default `hermes-rules.local`) |
| `HERMES_BASE_URL` | OpenAI-compatible base, including `/v1` |
| `HERMES_API_KEY` / `HERMES_MODEL` | Gateway auth and model name |
| `APPLE_INTELLIGENCE` | Use `siri` for briefing / ask / email triage (default true) |
| `SIRI_BIN` / `SIRI_SHORTCUT` | Siri CLI path and Shortcuts name |
| `OBSIDIAN_MCP_URL` / `OBSIDIAN_MCP_TOKEN` | Streamable HTTP MCP |
| `OBSIDIAN_VAULT_PATH` | Absolute vault path for dataview file scans (empty disables) |
| `OBSIDIAN_DAILY_FOLDER` | Vault-relative daily-note directory |
| `OBSIDIAN_VAULT_NAME` | Vault name for Advanced URI links (default `My Vault`) |
| `OBSIDIAN_AGENT_RECEIPT_FOLDER` | Where Gladys writes success receipts |
| `OBSIDIAN_DAILY_FORMAT` | Filename pattern (e.g. `D-YYYY-MM-DD`) |
| `OBSIDIAN_TEMPLATE_PATH` | Vault-relative Daily Template |
| `OMNIFOCUS_MCP_COMMAND` | OmniFocus MCP executable |
| `OMNIFOCUS_ON_DECK_PERSPECTIVE` | Perspective name for the task list |
| `OMNIFOCUS_TOMORROW_PERSPECTIVE` | Used when building a next-day briefing |
| `OMNIFOCUS_AGENT_ENABLED` | Background Gladys runner (default true) |
| `OMNIFOCUS_AGENT_TAG` | Assignment tag (default `🤖 Gladys`) |
| `OMNIFOCUS_AGENT_POLL_SECONDS` | Pickup interval (default `900`) |
| `OMNIFOCUS_AGENT_TIMEOUT_SECONDS` | Gladys Hermes wait; `0` means no client deadline |
| `OMNIFOCUS_REVIEW_TAG` | Tag on the follow-up review action (default `🔎 Review`) |
| `FANTASTICAL_MCP_COMMAND` | Fantastical MCP helper binary |
| `PUSHOVER_USER_KEY` / `PUSHOVER_API_KEY` | Optional briefing notifications |
| `BRIEFING_NOTE_PATH` | Optional markdown file written on each published briefing |
| `KIKODO_CRM_*` | Optional CRM MCP paths (configured, not wired into the current UI) |

Do not commit `.env`, `hermes-rules.local`, or other `*.local` files. Root `.gitignore` also excludes `*.pem` and `*.key`. Tokens for Hermes, Obsidian, Postgres, and Pushover live in `.env`.

**Local prompt rules:** copy [`hermes-rules.local.example`](hermes-rules.local.example) to `hermes-rules.local` and edit. Placeholders include `{weekday}`, `{date}`, `{human}`, `{timezone}`, `{time_of_day}`, `{is_shabbat}`, `{next_shabbat_starts_on}`. Without that file, Hermes prompts use a generic clock-only preamble.

## API surface

Routers are mounted under `/api`:

- `GET /api/health`
- `GET|PUT /api/preferences/timezone`
- `GET /api/calendar/today` and `GET /api/calendar?from=&to=`
- `GET /api/tasks/on-deck`, `GET /api/tasks/status`, `GET /api/tasks/agent`, `POST /api/tasks`, `POST /api/tasks/{id}/complete`, `POST /api/tasks/{id}/incomplete`
- `GET|PATCH /api/note/today`
- `GET|POST /api/briefing` (`?force=true` regenerates)
- `POST /api/agent/ask`
- `GET|POST /api/email/triage`, `POST /api/email/send`, `POST /api/email/delete`
- `POST /api/dataview/resolve`
- `GET /api/widgets`, `GET /api/widgets/{id}`
- `GET /api/events` (SSE)

Task complete/incomplete writes are also recorded in the `action_journal` table when Postgres is up; a journal failure does not block OmniFocus.

## Layout

```
backend/app/              FastAPI app, routers, MCP client, services
backend/app/localtime.py  Timezone resolution + preference hydration
backend/tests/            pytest
frontend/src/             React UI (see frontend/README.md)
scripts/run.sh              venv + uvicorn
scripts/build-frontend.sh
scripts/resolve_lnkd.py     Optional lnkd.in → destination rewriter for Markdown notes
launchd/                    launch agent plist
.env.example                template for local secrets and paths
hermes-rules.local.example  template for gitignored prompt rules
```
