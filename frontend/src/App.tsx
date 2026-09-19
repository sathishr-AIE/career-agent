import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import type { RunStatusContext } from './api'
import { Sidebar, navItemFor } from './components/Sidebar'
import { ShellContext, type Toast } from './components/shell'
import { usePoll } from './components/usePoll'
import './App.css'

const TOAST_MS = 5000

/** The application shell (approved Screen 1): sidebar, a 56px top bar with
 * the breadcrumb and the page's actions, the scrolling page area, and the
 * toast stack. At 720px and below the sidebar is a sheet, opened from the
 * top bar and closed by navigating, Escape or the scrim. */
export function App() {
  const { pathname } = useLocation()
  // The sheet stays open only on the page it was opened from, so any
  // navigation (a nav item, Answer) closes it without an effect.
  const [openOn, setOpenOn] = useState<string | null>(null)
  const navOpen = openOn === pathname
  const here = navItemFor(pathname)

  const run = usePoll<RunStatusContext>('/api/run/status').data
  const [actions, setActions] = useState<HTMLElement | null>(null)
  const [toasts, setToasts] = useState<(Toast & { id: number })[]>([])
  const nextToast = useRef(0)
  const dismiss = useCallback((id: number) => setToasts((ts) => ts.filter((t) => t.id !== id)), [])
  const toast = useCallback(
    (t: Toast) => {
      const id = nextToast.current++
      setToasts((ts) => [...ts, { ...t, id }])
      setTimeout(() => dismiss(id), TOAST_MS)
    },
    [dismiss],
  )
  const shell = useMemo(() => ({ run, toast, actions }), [run, toast, actions])

  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenOn(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  return (
    <ShellContext.Provider value={shell}>
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
            <div className="topbar__actions" ref={setActions} />
          </header>
          <div className="app__scroll">
            <main className="app__page">
              <Outlet />
            </main>
          </div>
        </div>
        <div className="toasts" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={t.tone === 'error' ? 'toast toast--error' : 'toast'}
                 role={t.tone === 'error' ? 'alert' : 'status'}>
              {t.tone === 'error' ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                     strokeLinecap="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="9" />
                  <path d="M12 8v4M12 16v.5" />
                </svg>
              ) : (
                <span className="toast__ok">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}
                       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M5 12l5 5 9-10" />
                  </svg>
                </span>
              )}
              <span className="toast__text">{t.text}</span>
              <button type="button" className="toast__x" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                     strokeLinecap="round" aria-hidden="true">
                  <path d="M7 7l10 10M17 7L7 17" />
                </svg>
              </button>
            </div>
          ))}
        </div>
      </div>
    </ShellContext.Provider>
  )
}
