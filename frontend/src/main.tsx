import { StrictMode, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { Route, RouterProvider, Routes, createBrowserRouter } from 'react-router-dom'
// Global styles first, so every component's own stylesheet loads after them
// and can override a shared primitive.
import './tokens.css'
import './components/ui.css'
import './legacy.css'
import { App } from './App'
import { Applications } from './routes/Applications'
import { Chat } from './routes/Chat'
import { Dashboard } from './routes/Dashboard'
import { Facts } from './routes/Facts'
import { Logins } from './routes/Logins'
import { Memory } from './routes/Memory'
import { Profile } from './routes/Profile'
import { Resumes } from './routes/Resumes'
import { Settings } from './routes/Settings'

// Pages not yet rebuilt to the redesign keep their old styles, scoped under
// .legacy (display: contents). Each screen drops its wrapper when it ships.
const legacy = (page: ReactNode) => <div className="legacy">{page}</div>

/** A data router around the <Routes> tree below (one splat route renders it):
 * FormPage's leave guard needs useBlocker, which only works under one. */
function DataRouter({ children }: { children: ReactNode }) {
  const [router] = useState(() => createBrowserRouter([{ path: '*', element: children }]))
  return <RouterProvider router={router} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DataRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={legacy(<Chat />)} />
          <Route path="chat/:id" element={legacy(<Chat />)} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="applications" element={<Applications />} />
          <Route path="resumes" element={<Resumes />} />
          <Route path="facts" element={<Facts />} />
          <Route path="memory" element={legacy(<Memory />)} />
          <Route path="logins" element={legacy(<Logins />)} />
          <Route path="profile" element={<Profile />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </DataRouter>
  </StrictMode>,
)
