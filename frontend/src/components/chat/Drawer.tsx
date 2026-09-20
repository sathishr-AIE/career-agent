import { useEffect, type ReactNode } from 'react'
import { Logins } from '../../routes/Logins'
import { Memory } from '../../routes/Memory'

/** The old pages, unchanged, in a right-side panel. They still own their
 * own routes -- this only gives them a second home next to the chat. */
const PANELS: Record<string, { title: string; el: () => ReactNode }> = {
  memory: { title: 'Memory', el: () => <Memory /> },
  logins: { title: 'Logins', el: () => <Logins /> },
}

export const PANEL_KEYS = Object.keys(PANELS)

export function panelTitle(panel: string) {
  return PANELS[panel]?.title ?? panel
}

export function Drawer({ panel, onClose }: { panel: string; onClose: () => void }) {
  const entry = PANELS[panel]

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (!entry) return null
  return (
    <aside className="drawer" role="dialog" aria-label={entry.title}>
      <header className="drawer__head">
        <h2>{entry.title}</h2>
        <button type="button" className="btn" onClick={onClose} aria-label="Close panel">
          Close
        </button>
      </header>
      <div className="drawer__body">{entry.el()}</div>
    </aside>
  )
}
