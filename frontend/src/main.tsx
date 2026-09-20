import { StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
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
import { Home } from './routes/Home'
import { Job } from './routes/Job'
import { JobDetails } from './routes/JobDetails'
import { Logins } from './routes/Logins'
import { Memory } from './routes/Memory'
import { Profile } from './routes/Profile'
import { Resumes } from './routes/Resumes'
import { Settings } from './routes/Settings'

// Pages not yet rebuilt to the redesign keep their old styles, scoped under
// .legacy (display: contents). Each screen drops its wrapper when it ships.
const legacy = (page: ReactNode) => <div className="legacy">{page}</div>

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={<Home />} />
          {/* The job chat keeps the pre-redesign page until Screen 6
              moves it into the job hub's Chat tab. */}
          <Route path="chat/:id" element={legacy(<Chat />)} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="applications" element={<Applications />} />
          {/* The job hub: Details now; Screen 6 adds path="chat". */}
          <Route path="jobs/:id" element={<Job />}>
            <Route index element={<JobDetails />} />
          </Route>
          <Route path="resumes" element={legacy(<Resumes />)} />
          <Route path="facts" element={legacy(<Facts />)} />
          <Route path="memory" element={legacy(<Memory />)} />
          <Route path="logins" element={legacy(<Logins />)} />
          <Route path="profile" element={legacy(<Profile />)} />
          <Route path="settings" element={legacy(<Settings />)} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
