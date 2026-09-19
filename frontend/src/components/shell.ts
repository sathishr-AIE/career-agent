import { createContext, useContext, type ReactNode } from 'react'
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
}

export const ShellContext = createContext<Shell>({ run: null, toast: () => {}, actions: null })

export const useShell = () => useContext(ShellContext)

/** A page's top-bar actions, rendered right of the breadcrumb (approved Shell). */
export function TopBarActions({ children }: { children: ReactNode }) {
  const { actions } = useShell()
  return actions ? createPortal(children, actions) : null
}
