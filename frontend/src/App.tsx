import { NavLink, Outlet } from 'react-router-dom'
import { AgentStatusBar } from './components/AgentStatusBar'
import { Glass } from './components/Glass'
import './App.css'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/applications', label: 'Applications' },
  { to: '/resumes', label: 'Resumes' },
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
