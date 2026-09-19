import { useEffect, useRef, useState } from 'react'
import { P } from '../icons'
import { Icon } from './Icon'

export interface MenuItem {
  label: string
  icon?: string
  danger?: boolean
  disabled?: boolean
  onSelect: () => void
}

/** The ⋯ row menu (approved Applications artboards). Closes on an outside
 * click, Escape or a choice. `note` is an optional line of context above the
 * items, e.g. a held job's warning. */
export function Menu({ items, note, label = 'More actions' }: {
  items: (MenuItem | 'sep')[]
  note?: string
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <span className="menu-wrap" ref={box}>
      <button type="button" className={open ? 'ib is-open' : 'ib'} aria-label={label}
              aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon d={P.dots} size={16} width={3.2} />
      </button>
      {open && (
        <div className="menu" role="menu">
          {note && <div className="menu__note">{note}</div>}
          {items.map((it, i) =>
            it === 'sep' ? (
              <div key={`sep-${i}`} className="menu__sep" role="separator" />
            ) : (
              <button key={it.label} type="button" role="menuitem" disabled={it.disabled}
                      className={it.danger ? 'menu__item menu__item--danger' : 'menu__item'}
                      onClick={() => {
                        setOpen(false)
                        it.onSelect()
                      }}>
                {it.icon && <Icon d={it.icon} />}
                {it.label}
              </button>
            ),
          )}
        </div>
      )}
    </span>
  )
}
