import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useBeforeUnload, useBlocker } from 'react-router-dom'
import './forms.css'

// The form-page kit Profile and Settings share: fields, a section nav with
// completion marks, the error summary, the sticky save bar and a leave guard.

/** The page while its first GET is in flight: header, nav and card skeletons. */
export function FormSkeleton({ title }: { title: string }) {
  return (
    <div className="page form-page" aria-busy="true">
      <div className="page-head"><h1>{title}</h1></div>
      <div className="form-layout">
        <div className="form-skel__nav">
          {[70, 55, 60, 75, 65].map((w) => <span className="skel" key={w} style={{ width: `${w}%` }} />)}
        </div>
        <div className="form-sections">
          {[0, 1, 2].map((i) => (
            <div className="card form-skel__card" key={i}>
              {[35, 90, 90, 60].map((w, j) => <span className="skel" key={j} style={{ width: `${w}%` }} />)}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/** The page when its first GET failed: the server's message and Retry. */
export function LoadError({ title, what, error, onRetry }: {
  title: string
  what: string
  error: string
  onRetry: () => void
}) {
  return (
    <div className="page form-page">
      <div className="page-head"><h1>{title}</h1></div>
      <div className="alert" role="alert">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
             strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v5M12 16v.5" />
        </svg>
        <span><b>Can't load {what}:</b> {error}</span>
        <button type="button" className="btn form-page__retry" onClick={onRetry}>Retry</button>
      </div>
    </div>
  )
}

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
export function TextInput({ id, label, value, onChange, error, required, help, span2, mono, short, type = 'text',
  min, placeholder, disabled }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  error?: string
  required?: boolean
  help?: string
  span2?: boolean
  mono?: boolean
  /** A 120px box, for small numbers. */
  short?: boolean
  type?: string
  min?: number
  placeholder?: string
  disabled?: boolean
}) {
  const extra = [mono && 'mono', short && 'short'].filter(Boolean).join(' ')
  return (
    <Field id={id} label={label} required={required} error={error} help={help} span2={span2}>
      <input id={id} type={type} className={inCls(error, extra)} value={value} min={min}
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
      // Scrolled all the way down (a page that doesn't scroll isn't "at the bottom").
      const atBottom = scroller.scrollTop > 0
        && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2
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

/** A list as chips. Enter or a comma adds one, × removes one, Backspace in an
 * empty input removes the last; a pasted comma list splits, and text still
 * typed when the field loses focus is kept as a chip. Chips never contain a
 * comma, so the page can send the list comma-joined (the settings API's shape). */
export function TagInput({ id, label, value, onChange, error, required, help, placeholder }: {
  id: string
  label: string
  value: string[]
  onChange: (v: string[]) => void
  error?: string
  required?: boolean
  help?: string
  placeholder?: string
}) {
  const [draft, setDraft] = useState('')
  const add = (text: string) => {
    const parts = text.split(',').map((s) => s.trim()).filter(Boolean)
    const fresh = parts.filter((p, i) => !value.includes(p) && parts.indexOf(p) === i)
    if (fresh.length) onChange([...value, ...fresh])
    setDraft('')
  }
  return (
    <Field id={id} label={label} required={required} error={error} help={help}>
      <div className={`${inCls(error)} tags`} onClick={() => document.getElementById(id)?.focus()}>
        {value.map((t) => (
          <span className="tag" key={t}>
            {t}
            <button type="button" aria-label={`Remove ${t}`} onClick={() => onChange(value.filter((x) => x !== t))}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}
                   strokeLinecap="round" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" /></svg>
            </button>
          </span>
        ))}
        <input id={id} value={draft} placeholder={value.length ? undefined : placeholder}
               aria-invalid={error ? true : undefined} aria-describedby={error ? `${id}-err` : undefined}
               onChange={(e) => (e.target.value.includes(',') ? add(e.target.value) : setDraft(e.target.value))}
               onKeyDown={(e) => {
                 if (e.key === 'Enter') {
                   e.preventDefault() // a chip, not a form submit
                   add(draft)
                 } else if (e.key === 'Backspace' && !draft && value.length) {
                   onChange(value.slice(0, -1))
                 }
               }}
               onBlur={() => add(draft)} />
      </div>
    </Field>
  )
}

/** A native checkbox drawn as a switch. */
export function Switch({ label, checked, onChange }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  )
}

/** A number input with − and + buttons (the value stays a string, as typed). */
export function Stepper({ id, label, value, onChange, min, error, help }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  min: number
  error?: string
  help?: string
}) {
  const step = (d: number) => onChange(String(Math.max(min, (parseInt(value, 10) || min) + d)))
  return (
    <Field id={id} label={label} error={error} help={help}>
      <span className={`num-stepper${error ? ' bad' : ''}`}>
        <button type="button" aria-label={`Decrease ${label}`} onClick={() => step(-1)}>−</button>
        <input id={id} type="number" min={min} className="mono" value={value}
               aria-invalid={error ? true : undefined} onChange={(e) => onChange(e.target.value)} />
        <button type="button" aria-label={`Increase ${label}`} onClick={() => step(1)}>+</button>
      </span>
    </Field>
  )
}

/** A 0–max range slider paired with a mono number input; both edit one value. */
export function Slider({ id, label, value, onChange, max = 100, error, help }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  max?: number
  error?: string
  help?: string
}) {
  const n = Math.min(max, Math.max(0, parseInt(value, 10) || 0))
  return (
    <Field id={id} label={label} error={error} help={help}>
      <span className="slider">
        <input type="range" min={0} max={max} value={n} aria-label={label}
               style={{ ['--pct' as string]: `${(n / max) * 100}%` }} onChange={(e) => onChange(e.target.value)} />
        <input id={id} type="number" min={0} max={max} className={inCls(error, 'mono')} value={value}
               aria-invalid={error ? true : undefined} onChange={(e) => onChange(e.target.value)} />
      </span>
    </Field>
  )
}

/** One choice per card: a label and the mono value (a model id). */
export function RadioCards({ name, label, options, labels, value, onChange, error, help }: {
  name: string
  label: string
  options: string[]
  labels: Record<string, string>
  value: string
  onChange: (v: string) => void
  error?: string
  help?: string
}) {
  return (
    <fieldset className="f radio-cards" id={name}>
      <legend className="fl">{label}</legend>
      <div className="radio-cards__grid">
        {options.map((o) => (
          <label key={o} className={o === value ? 'radio-card on' : 'radio-card'}>
            <input type="radio" name={name} value={o} checked={o === value} onChange={() => onChange(o)} />
            <span className="radio-card__text">
              <span className="radio-card__label">{labels[o] ?? o}</span>
              <span className="mono radio-card__value">{o}</span>
            </span>
          </label>
        ))}
      </div>
      {error ? <span className="err">{error}</span> : help && <span className="help">{help}</span>}
    </fieldset>
  )
}
