import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar, navItemFor } from './components/Sidebar'
import './App.css'

/** The application shell (approved Screen 1): sidebar, a 56px top bar with
 * the breadcrumb, and the scrolling page area. At 720px and below the sidebar
 * is a sheet, opened from the top bar and closed by navigating, Escape or the
 * scrim. */
export function App() {
  const { pathname } = useLocation()
  // The sheet stays open only on the page it was opened from, so any
  // navigation (a nav item, Answer) closes it without an effect.
  const [openOn, setOpenOn] = useState<string | null>(null)
  const navOpen = openOn === pathname
  const here = navItemFor(pathname)

  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenOn(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  return (
    <div className="app" data-nav-open={navOpen || undefined}>
      <Sidebar />
      {navOpen && <div className="scrim" onClick={() => setOpenOn(null)} />}
      <div className="app__main">
        <header className="topbar">
          <div className="topbar__left">
            <button type="button" className="ib topbar__menu" aria-label="Open navigation"
                    aria-expanded={navOpen} onClick={() => setOpenOn(pathname)}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                   strokeLinecap="round" aria-hidden="true">
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <nav className="crumbs" aria-label="Breadcrumb">
              {here && (
                <>
                  <span>{here.group}</span>
                  <span className="crumbs__sep mono" aria-hidden="true">›</span>
                </>
              )}
              <span className="crumbs__here" aria-current="page">{here?.label ?? 'Page not found'}</span>
            </nav>
          </div>
        </header>
        <div className="app__scroll">
          <main className="app__page">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  )
}
