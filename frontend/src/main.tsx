import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { App } from './App'
import { Applications } from './routes/Applications'
import { Chat } from './routes/Chat'
import { Dashboard } from './routes/Dashboard'
import { Resumes } from './routes/Resumes'
import { Settings } from './routes/Settings'
import './tokens.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={<Chat />} />
          <Route path="chat/:id" element={<Chat />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="applications" element={<Applications />} />
          <Route path="resumes" element={<Resumes />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
