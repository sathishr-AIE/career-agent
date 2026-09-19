import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { P } from '../icons'
import { Icon } from './Icon'

/** The component sheet's confirm dialog on a native <dialog>, which gives the
 * focus trap, Escape and backdrop for free. Render it only while open. It is
 * portalled to <body>, so a caller inside a table can't nest it invalidly. */
export function ConfirmDialog({ title, text, confirm, busy = false, onConfirm, onCancel }: {
  title: string
  text: string
  confirm: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const d = ref.current
    if (d && !d.open) d.showModal()   // StrictMode runs this twice; showModal on an open dialog throws
  }, [])

  return createPortal(
    <dialog ref={ref} className="dialog" aria-labelledby="dialog-title"
            onCancel={(e) => {
              e.preventDefault()
              onCancel()
            }}>
      <div className="dialog__body">
        <span className="dialog__icon"><Icon d={P.alert} size={18} /></span>
        <div>
          <h2 id="dialog-title" className="dialog__title">{title}</h2>
          <p className="dialog__text">{text}</p>
        </div>
      </div>
      <div className="dialog__actions">
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button type="button" className="btn danger" disabled={busy} onClick={onConfirm}>{confirm}</button>
      </div>
    </dialog>,
    document.body,
  )
}
