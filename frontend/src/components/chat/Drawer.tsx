import { useEffect, type ReactNode } from 'react'
import { Applications } from '../../routes/Applications'
import { Resumes } from '../../routes/Resumes'
import { Settings } from '../../routes/Settings'

/** The old pages, unchanged, in a right-side panel. They still own their
 * own routes -- this only gives them a second home next to the chat. */
const PANELS: Record<string, { title: string; el: () => ReactNode }> = {
  applications: { title: 'Queue', el: () => <Applications /> },
  resumes: { title: 'Résumés', el: () => <Resumes /> },
  settings: { title: 'Settings', el: () => <Settings /> },
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
