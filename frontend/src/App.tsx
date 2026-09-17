import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BriefingPanel, StatusPanel } from './components/BriefingPanel'
import { CalendarPanel } from './components/CalendarPanel'
import { DailyNotePanel } from './components/DailyNotePanel'
import { EmailPanel } from './components/EmailPanel'
import { HermesBar, useSseRefresh } from './components/HermesBar'
import { OnDeckPanel } from './components/OnDeckPanel'
import './App.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
})

function Dashboard() {
  useSseRefresh()
  const today = new Date().toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div>
          <p className="brand">Hermes Dashboard</p>
          <h1>{today}</h1>
        </div>
        <HermesBar />
      </header>

      <main className="layout">
        <div className="main-column">
          <BriefingPanel />
          <EmailPanel />
          <DailyNotePanel />
        </div>
        <div className="status-column">
          <StatusPanel />
          <CalendarPanel />
        </div>
        <aside className="tasks-column">
          <OnDeckPanel />
        </aside>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  )
}
