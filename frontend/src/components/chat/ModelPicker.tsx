import { useEffect, useRef, useState } from 'react'
import { Icon } from '../Icon'
import { P } from '../../icons'

/** "claude-haiku-4-5" -> "Haiku 4.5". MODEL_LABELS holds whole sentences
 * ("Haiku -- cheaper and faster"), which don't fit the pill. */
function shortModel(id: string): string {
  const [family, ...rest] = id.replace(/^claude-/, '').split('-')
  const name = family.charAt(0).toUpperCase() + family.slice(1)
  return rest.length ? `${name} ${rest.join('.')}` : name
}

/** The composer's model chip. Dumb on purpose: the page owns the fetch and
 * the PUT, so Screen 6 can hand it the apply model and a locked state
 * without touching this file. Its own popover rather than Menu.tsx, which
 * would need a custom trigger and an open-upward mode for one caller. */
export function ModelPicker({ label, value, options, optionLabel, onPick, busy = false,
                             lockedText }: {
  label: string
  value: string | null
  options: string[]
  optionLabel?: (id: string) => string
  onPick: (id: string) => void
  busy?: boolean
  lockedText?: string
}) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  if (lockedText) {
    return (
      <span className="chip chip--locked" title={lockedText}>
        <Icon d={P.lock} size={12} />
        {lockedText}
      </span>
    )
  }

  return (
    <div className="menu-wrap" ref={wrap}>
      <button type="button" className="chip chip--pick" aria-expanded={open} disabled={busy}
              onClick={() => setOpen((o) => !o)}>
        {label}
        <span className="chip__sep">·</span>
        <span className="mono">{value ? shortModel(value) : '—'}</span>
        <Icon d={P.chevronDown} size={12} />
      </button>
      {open && (
        <div className="menu menu--up" role="menu">
          {options.map((id) => (
            <button key={id} type="button" className="menu__item" role="menuitem"
                    onClick={() => {
                      setOpen(false)
                      if (id !== value) onPick(id)
                    }}>
              <span className="menu__tick">{id === value && <Icon d={P.check} size={14} />}</span>
              {optionLabel ? optionLabel(id) : shortModel(id)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
