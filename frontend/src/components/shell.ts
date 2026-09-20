import { createContext, useContext, useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { RunStatusContext } from '../api'

export interface Toast {
  text: string
  tone?: 'ok' | 'error'
}

interface Shell {
  /** /api/run/status, polled once by App for every consumer. */
  run: RunStatusContext | null
  toast: (t: Toast) => void
  /** The top bar's right-hand slot; pages portal their actions into it. */
  actions: HTMLElement | null
  /** A page's own breadcrumb after its nav item (set through useCrumb). */
  setCrumb: (crumb: string | null) => void
}

export const ShellContext = createContext<Shell>({
  run: null, toast: () => {}, actions: null, setCrumb: () => {},
})

export const useShell = () => useContext(ShellContext)

/** Adds a page's own last breadcrumb, e.g. "Applications › Stripe · SRE";
 * cleared when the page unmounts. Pass null while it isn't known yet. */
export function useCrumb(crumb: string | null) {
  const { setCrumb } = useShell()
  useEffect(() => {
    setCrumb(crumb)
    return () => setCrumb(null)
  }, [crumb, setCrumb])
}

/** A page's top-bar actions, rendered right of the breadcrumb (approved Shell). */
export function TopBarActions({ children }: { children: ReactNode }) {
  const { actions } = useShell()
  return actions ? createPortal(children, actions) : null
}
