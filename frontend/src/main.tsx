import { StrictMode, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { Route, RouterProvider, Routes, createBrowserRouter } from 'react-router-dom'
// Global styles first, so every component's own stylesheet loads after them
// and can override a shared primitive.
import './tokens.css'
import './components/ui.css'
import { App } from './App'
import { Applications } from './routes/Applications'
import { Dashboard } from './routes/Dashboard'
import { Facts } from './routes/Facts'
import { Home } from './routes/Home'
import { ChatRedirect, JobChat } from './routes/JobChat'
import { Job } from './routes/Job'
import { JobDetails } from './routes/JobDetails'
import { Logins } from './routes/Logins'
import { Memory } from './routes/Memory'
import { Profile } from './routes/Profile'
import { Resumes } from './routes/Resumes'
import { Settings } from './routes/Settings'

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
          <Route index element={<Home />} />
          {/* Links and bookmarks from before the job chat moved into the hub. */}
          <Route path="chat/:id" element={<ChatRedirect />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="applications" element={<Applications />} />
          <Route path="jobs/:id" element={<Job />}>
            <Route index element={<JobDetails />} />
            <Route path="chat" element={<JobChat />} />
          </Route>
          <Route path="resumes" element={<Resumes />} />
          <Route path="facts" element={<Facts />} />
          <Route path="memory" element={<Memory />} />
          <Route path="logins" element={<Logins />} />
          <Route path="profile" element={<Profile />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </DataRouter>
  </StrictMode>,
)
