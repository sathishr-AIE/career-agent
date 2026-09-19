import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useBeforeUnload, useBlocker } from 'react-router-dom'
import './forms.css'

// The form-page kit Profile and Settings share: fields, a section nav with
// completion marks, the error summary, the sticky save bar and a leave guard.

/** Classes for an `.in` control: the error ring when `error` is set. */
export const inCls = (error?: string, extra = '') => `in${error ? ' bad' : ''}${extra ? ` ${extra}` : ''}`

/** Label, control, then its error (or help text). The control is `children`;
 * give it `id` so the label points at it and ErrorSummary can focus it. */
export function Field({ id, label, required, error, help, span2, children }: {
  id: string
  label: string
  required?: boolean
  error?: string
  help?: string
  span2?: boolean
  children: ReactNode
}) {
  return (
    <div className={span2 ? 'f span2' : 'f'}>
      <label className="fl" htmlFor={id}>
        {label}
        {required && <span className="req"> *</span>}
      </label>
      {children}
      {error ? <span className="err" id={`${id}-err`}>{error}</span> : help && <span className="help">{help}</span>}
    </div>
  )
}

/** The common case: a text-like input in a Field. */
export function TextInput({ id, label, value, onChange, error, required, help, span2, mono, type = 'text',
  placeholder, disabled }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  error?: string
  required?: boolean
  help?: string
  span2?: boolean
  mono?: boolean
  type?: string
  placeholder?: string
  disabled?: boolean
}) {
  return (
    <Field id={id} label={label} required={required} error={error} help={help} span2={span2}>
      <input id={id} type={type} className={inCls(error, mono ? 'mono' : '')} value={value}
             placeholder={placeholder} disabled={disabled} aria-invalid={error ? true : undefined}
             aria-describedby={error ? `${id}-err` : undefined} onChange={(e) => onChange(e.target.value)} />
    </Field>
  )
}

export interface NavSection {
  id: string
  label: string
  /** 'bad': a field in it has an error. 'done': complete. */
  mark?: 'bad' | 'done'
}

/** Anchors to the page's sections, highlighting the one being read: the last
 * whose top has scrolled past a line near the top of the shell's `.app__scroll`
 * (or the last section at the very bottom). At 720px and below it is a chip row. */
export function SectionNav({ sections, label }: { sections: NavSection[]; label: string }) {
  const [active, setActive] = useState(sections[0]?.id)
  // A clicked section stays highlighted while its smooth scroll settles, even
  // when the page bottoms out before that section reaches the top.
  const clickedAt = useRef(0)
  const ids = sections.map((s) => s.id).join()

  useEffect(() => {
    const scroller = document.querySelector('.app__scroll')
    if (!scroller) return
    const order = ids.split(',')
    const onScroll = () => {
      if (Date.now() - clickedAt.current < 1000) return
      const line = scroller.getBoundingClientRect().top + 120 // clears the sticky chip row too
      const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2
      const passed = order.filter((id) => (document.getElementById(id)?.getBoundingClientRect().top ?? 1e9) <= line)
      setActive(atBottom ? order[order.length - 1] : passed[passed.length - 1] ?? order[0])
    }
    onScroll()
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [ids])

  // Keep the active chip visible in the narrow-width chip row.
  useEffect(() => {
    document.querySelector('.section-nav [aria-current="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [active])

  return (
    <nav className="section-nav" aria-label={label}>
      {sections.map((s) => (
        <button key={s.id} type="button" aria-current={s.id === active ? 'true' : undefined}
                onClick={() => {
                  clickedAt.current = Date.now()
                  setActive(s.id)
                  document.getElementById(s.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                }}>
          {s.label}
          {s.mark === 'bad' && <span className="section-nav__bad" role="img" aria-label="needs attention" />}
          {s.mark === 'done' && (
            <svg className="section-nav__done" width="14" height="14" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round"
                 role="img" aria-label="complete">
              <path d="M5 12l5 5 9-10" />
            </svg>
          )}
        </button>
      ))}
    </nav>
  )
}

export interface SummaryItem {
  label: string
  /** The control to focus; none for a message that isn't about one field. */
  target?: string
}

/** "Nothing was saved — N fields need attention: Phone, Work history · Title",
 * each name focusing its field. */
export function ErrorSummary({ items }: { items: SummaryItem[] }) {
  if (!items.length) return null
  const n = items.length
  return (
    <div className="alert" role="alert">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
           strokeLinecap="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7.5v5M12 16v.5" />
      </svg>
      <span>
        <b>Nothing was saved — {n} {n === 1 ? 'field needs' : 'fields need'} attention:</b>{' '}
        {items.map((it, i) => (
          <span key={`${it.label}-${i}`}>
            {i > 0 && ', '}
            {it.target ? (
              <a href={`#${it.target}`} onClick={(e) => {
                e.preventDefault()
                const el = document.getElementById(it.target!)
                el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                el?.focus({ preventScroll: true })
              }}>{it.label}</a>
            ) : it.label}
          </span>
        ))}
      </span>
    </div>
  )
}

/** Shown only while the form differs from its last load or save. Save is a
 * submit button, so the page's <form onSubmit> owns saving (Enter works too). */
export function SaveBar({ dirty, busy, saveLabel, onDiscard }: {
  dirty: boolean
  busy: boolean
  saveLabel: string
  onDiscard: () => void
}) {
  if (!dirty) return null
  return (
    <div className="save-bar">
      <span className="save-bar__note"><span className="dot dot--lg dot--am" />Unsaved changes</span>
      <span className="save-bar__actions">
        <button type="button" className="btn" disabled={busy} onClick={onDiscard}>Discard</button>
        <button type="submit" className="btn pri" disabled={busy}>
          {busy && (
            <svg className="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity={0.35} strokeWidth={3} />
              <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth={3} strokeLinecap="round" />
            </svg>
          )}
          {saveLabel}
        </button>
      </span>
    </div>
  )
}

/** Asks before leaving the page (a nav link, back, closing the tab) while
 * `dirty`. Needs main.tsx's data router for useBlocker. */
export function useLeaveGuard(dirty: boolean) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirty && currentLocation.pathname !== nextLocation.pathname)

  useEffect(() => {
    if (blocker.state !== 'blocked') return
    // ponytail: the browser's own confirm; swap in Screen 3's ConfirmDialog once it merges.
    if (window.confirm('Leave without saving? Your unsaved changes will be lost.')) blocker.proceed()
    else blocker.reset()
  }, [blocker])

  useBeforeUnload(
    useCallback((e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault()
    }, [dirty]),
  )
}
