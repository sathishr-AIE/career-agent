import { NavLink, Outlet } from 'react-router-dom'
import { AgentStatusBar } from './components/AgentStatusBar'
import { Glass } from './components/Glass'
import './App.css'

const NAV = [
  { to: '/', label: 'Chat', end: true },
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/applications', label: 'Applications' },
  { to: '/resumes', label: 'Resumes' },
  { to: '/facts', label: 'Facts' },
  { to: '/memory', label: 'Memory' },
  { to: '/logins', label: 'Logins' },
  { to: '/profile', label: 'Profile' },
  { to: '/settings', label: 'Settings' },
]

export function App() {
  return (
    <div className="shell">
      <Glass as="nav" className="sidebar">
        <div className="sidebar__brand">Career Agent</div>
        <div className="sidebar__nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? 'active' : undefined)}
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      </Glass>
      <div className="plane">
        <div className="plane__content">
          <div className="plane__inner">
            <Outlet />
          </div>
        </div>
        <AgentStatusBar />
      </div>
    </div>
  )
}
