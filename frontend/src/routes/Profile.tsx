import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, errorText, get, put } from '../api'
import {
  ErrorSummary, Field, FormSkeleton, LoadError, SaveBar, SectionNav, TextInput, inCls, useLeaveGuard,
  type NavSection, type SummaryItem,
} from '../components/FormPage'
import { Icon } from '../components/Icon'
import { useShell } from '../components/shell'
import { P } from '../icons'
import './Profile.css'

// -- shapes /api/profile returns (web/api_profile.py, config.CandidateProfile) --

interface Address {
  line1: string
  city: string
  state: string
  postal_code: string
  country: string
}

interface WorkEntry {
  company: string
  title: string
  start: string
  end: string
  current: boolean
  description: string
}

interface EduEntry {
  institution: string
  degree: string
  field: string
  start: string
  end: string
}

interface CandidateProfile {
  candidate_name: string
  candidate_email: string
  candidate_phone: string
  linkedin_url: string | null
  portfolio_url: string | null
  gender: string
  address: Address
  work_history: WorkEntry[]
  education: EduEntry[]
}

// The API never sees `id` -- it exists only so React has a stable key across
// reorders/removals, and so a server error can find its row (remapRowErrors).
interface WorkRow extends WorkEntry { id: string }
interface EduRow extends EduEntry { id: string }

interface ProfileForm extends Omit<CandidateProfile, 'work_history' | 'education'> {
  work_history: WorkRow[]
  education: EduRow[]
}

const GENDERS: [string, string][] = [
  ['decline', 'Decline to state'], ['female', 'Female'], ['male', 'Male'], ['non-binary', 'Non-binary'],
]

const CONTACT: [keyof CandidateProfile, string][] = [
  ['candidate_name', 'Full name'], ['candidate_email', 'Email'], ['candidate_phone', 'Phone'],
  ['linkedin_url', 'LinkedIn URL'], ['portfolio_url', 'Portfolio URL'],
]
const ADDRESS: [keyof Address, string][] = [
  ['line1', 'Address line 1'], ['city', 'City'], ['state', 'State'], ['postal_code', 'Postal code'],
  ['country', 'Country'],
]
const WORK_LABELS: Record<string, string> = {
  company: 'Company', title: 'Title', start: 'Start', end: 'End', current: 'I currently work here',
  description: 'Description',
}
const EDU_LABELS: Record<string, string> = {
  institution: 'Institution', degree: 'Degree', field: 'Field', start: 'Start', end: 'End',
}

const newId = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `row-${Math.random().toString(36).slice(2)}`

const emptyWork = (): WorkRow => ({
  id: newId(), company: '', title: '', start: '', end: '', current: false, description: '',
})
const emptyEdu = (): EduRow => ({ id: newId(), institution: '', degree: '', field: '', start: '', end: '' })

const toForm = (p: CandidateProfile): ProfileForm => ({
  ...p,
  work_history: p.work_history.map((w) => ({ ...w, id: newId() })),
  education: p.education.map((e) => ({ ...e, id: newId() })),
})

// A row with every field still blank is dropped before sending, so an
// accidental Add needs no Remove. A row with anything in it is sent as is and,
// missing something required, shows that field's own error.
const isBlankWork = (w: WorkRow) => !w.company && !w.title && !w.start && !w.end && !w.description && !w.current
const isBlankEdu = (e: EduRow) => !e.institution && !e.degree && !e.field && !e.start && !e.end

function move<T>(list: T[], index: number, delta: number): T[] {
  const target = index + delta
  if (target < 0 || target >= list.length) return list
  const next = [...list]
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}

/** The server reports errors against the *sent* (blank-row-filtered) array's
 * index, e.g. "work_history.0.company", which no longer matches the form's
 * index once a blank row upstream was dropped. Remap each to the row's stable
 * id, using the exact arrays that were sent. */
function remapRowErrors(raw: Record<string, string>, sentWork: WorkRow[], sentEdu: EduRow[]) {
  const out: Record<string, string> = {}
  for (const [key, message] of Object.entries(raw)) {
    const w = key.match(/^work_history\.(\d+)\.(.+)$/)
    const e = key.match(/^education\.(\d+)\.(.+)$/)
    if (w && sentWork[Number(w[1])]) out[`work:${sentWork[Number(w[1])].id}:${w[2]}`] = message
    else if (e && sentEdu[Number(e[1])]) out[`edu:${sentEdu[Number(e[1])].id}:${e[2]}`] = message
    else out[key] = message
  }
  return out
}

/** The error summary's names in page order, each pointing at its control's id. */
function summarize(errors: Record<string, string>, f: ProfileForm): SummaryItem[] {
  const rank = (key: string) => {
    const contact = CONTACT.findIndex(([k]) => k === key)
    if (contact >= 0) return contact
    if (key === 'gender') return 10
    const addr = key.match(/^address\.(.+)$/)
    if (addr) return 20 + ADDRESS.findIndex(([k]) => k === addr[1])
    const row = key.match(/^(work|edu):([^:]+):(.+)$/)
    if (!row) return 1e6
    const [rows, labels, base] = row[1] === 'work'
      ? [f.work_history, WORK_LABELS, 100] as const : [f.education, EDU_LABELS, 1e4] as const
    return base + rows.findIndex((r) => r.id === row[2]) * 10 + Object.keys(labels).indexOf(row[3])
  }
  const keys = Object.keys(errors).filter((k) => k !== 'form').sort((a, b) => rank(a) - rank(b))
  return keys.map((key) => {
    const contact = CONTACT.find(([k]) => k === key)
    if (contact) return { label: contact[1], target: key }
    if (key === 'gender') return { label: 'Gender', target: 'gender' }
    const addr = key.match(/^address\.(.+)$/)
    if (addr) {
      const label = ADDRESS.find(([k]) => k === addr[1])?.[1] ?? addr[1]
      return { label: `Address · ${label}`, target: `address_${addr[1]}` }
    }
    const row = key.match(/^(work|edu):([^:]+):(.+)$/)
    if (row) {
      const [, kind, id, field] = row
      const labels = kind === 'work' ? WORK_LABELS : EDU_LABELS
      return { label: `${kind === 'work' ? 'Work history' : 'Education'} · ${labels[field] ?? field}`,
               target: `${kind}_${id}_${field}` }
    }
    return { label: key }
  })
}

const dates = (start: string, end: string, current = false) =>
  !start && !end && !current ? '' : `${start || '…'} – ${current ? 'Present' : end || '…'}`

/** A native month picker (it already produces YYYY-MM). An older free-text
 * date a picker can't show stays a text input, so it is never silently hidden. */
function MonthInput({ id, label, value, onChange, error }: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  error?: string
}) {
  const picker = !value || /^\d{4}-\d{2}$/.test(value)
  return (
    <Field id={id} label={label} error={error}>
      <input id={id} type={picker ? 'month' : 'text'} className={inCls(error, 'mono')} value={value}
             aria-invalid={error ? true : undefined} onChange={(e) => onChange(e.target.value)} />
    </Field>
  )
}

/** One work or education row: collapsed to a summary line, or open with its
 * fields. Move up, Move down and Remove sit on the right either way. */
function Entry({ open, onToggle, heading, summary, first, last, onMove, onRemove, children }: {
  open: boolean
  onToggle: () => void
  heading: string
  summary: ReactNode
  first: boolean
  last: boolean
  onMove: (delta: number) => void
  onRemove: () => void
  children: ReactNode
}) {
  return (
    <div className={open ? 'entry entry--open' : 'entry'}>
      <div className="entry__head">
        <button type="button" className="entry__toggle" aria-expanded={open} onClick={onToggle}>
          <Icon d={P.chevronRight} />
          {open ? <span className="entry__name">{heading}</span> : summary}
        </button>
        <span className="entry__actions">
          <button type="button" className="ib" title="Move up" aria-label="Move up" disabled={first}
                  onClick={() => onMove(-1)}><Icon d={P.up} /></button>
          <button type="button" className="ib" title="Move down" aria-label="Move down" disabled={last}
                  onClick={() => onMove(1)}><Icon d={P.down} /></button>
          <button type="button" className="ib ib--danger" title="Remove" aria-label="Remove"
                  onClick={onRemove}><Icon d={P.trash} /></button>
        </span>
      </div>
      {open && <div className="grid2">{children}</div>}
    </div>
  )
}

function Summary({ title, meta, when }: { title: string; meta: string; when: string }) {
  return (
    <span className="entry__summary">
      <span className="entry__title">{title}</span>
      {(meta || when) && <span className="entry__meta">{meta ? `· ${meta}${when ? ' ·' : ''}` : '·'}</span>}
      {when && <span className="entry__dates mono">{when}</span>}
    </span>
  )
}

/** The candidate profile (approved Screen 9): the single source of the details
 * the apply agent types into forms. PUT sends the whole profile; the server
 * validates everything before writing anything. */
export function Profile() {
  const { toast } = useShell()
  const [form, setForm] = useState<ProfileForm | null>(null)
  // The last loaded or saved form, as JSON: the form is dirty while it differs.
  const [snapshot, setSnapshot] = useState('')
  const [exists, setExists] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [open, setOpen] = useState<Set<string>>(new Set())

  const dirty = form !== null && JSON.stringify(form) !== snapshot
  useLeaveGuard(dirty)

  const load = useCallback(
    () =>
      get<{ exists: boolean; profile: CandidateProfile }>('/api/profile')
        .then((r) => {
          const f = toForm(r.profile)
          setForm(f)
          setSnapshot(JSON.stringify(f))
          setExists(r.exists)
          setErrors({})
          setOpen(new Set())
          setLoadError(null)
        })
        .catch((e: unknown) => setLoadError(errorText(e))),
    [],
  )

  useEffect(() => {
    load()
  }, [load])

  // After a refused save, bring the first bad field into view.
  useEffect(() => {
    if (Object.keys(errors).length) {
      document.querySelector('.profile .in.bad')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [errors])

  if (!form) {
    return loadError
      ? <LoadError title="Profile" what="your profile" error={loadError} onRetry={load} />
      : <FormSkeleton title="Profile" />
  }

  const f = form
  const set = (patch: Partial<ProfileForm>) => setForm({ ...f, ...patch })
  const setAddress = (patch: Partial<Address>) => set({ address: { ...f.address, ...patch } })
  const setWork = (i: number, patch: Partial<WorkEntry>) =>
    set({ work_history: f.work_history.map((w, j) => (j === i ? { ...w, ...patch } : w)) })
  const setEdu = (i: number, patch: Partial<EduEntry>) =>
    set({ education: f.education.map((e, j) => (j === i ? { ...e, ...patch } : e)) })
  const toggle = (id: string) =>
    setOpen((o) => {
      const next = new Set(o)
      if (!next.delete(id)) next.add(id)
      return next
    })
  const addOpen = (id: string) => setOpen((o) => new Set(o).add(id))

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!dirty || saving) return
    setSaving(true)
    const sentWork = f.work_history.filter((w) => !isBlankWork(w))
    const sentEdu = f.education.filter((ed) => !isBlankEdu(ed))
    const payload = {
      ...f,
      work_history: sentWork.map(({ id: _id, ...rest }) => rest),
      education: sentEdu.map(({ id: _id, ...rest }) => rest),
    }
    try {
      await put<{ ok: boolean; message: string }>('/api/profile', payload)
      await load() // the server trims; reload so the snapshot is what's on disk
      toast({ text: 'Profile saved' })
    } catch (err) {
      const body = err instanceof ApiError ? (err.body as { errors?: Record<string, string> } | undefined) : undefined
      if (body?.errors) {
        const mapped = remapRowErrors(body.errors, sentWork, sentEdu)
        // Open every row that has an error, so the summary's links land on a field.
        setOpen((o) => new Set([...o, ...Object.keys(mapped).flatMap((k) => k.match(/^(?:work|edu):([^:]+):/)?.[1] ?? [])]))
        setErrors(mapped)
      } else {
        setErrors({ form: errorText(err) })
      }
    } finally {
      setSaving(false)
    }
  }

  const has = (prefix: string) => Object.keys(errors).some((k) => k.startsWith(prefix))
  const mark = (bad: boolean, done: boolean): NavSection['mark'] => (bad ? 'bad' : done ? 'done' : undefined)
  const sections: NavSection[] = [
    { id: 'profile-contact', label: 'Contact',
      mark: mark(CONTACT.some(([k]) => errors[k]),
                 !!(f.candidate_name.trim() && f.candidate_email.trim() && f.candidate_phone.trim())) },
    { id: 'profile-personal', label: 'Personal', mark: mark(!!errors.gender, !!f.gender.trim()) },
    { id: 'profile-address', label: 'Address',
      mark: mark(has('address.'), ADDRESS.every(([k]) => f.address[k].trim())) },
    { id: 'profile-work', label: 'Work history',
      mark: mark(has('work:') || has('work_history'),
                 f.work_history.length > 0 && f.work_history.every((w) => w.company.trim() && w.title.trim())) },
    { id: 'profile-education', label: 'Education',
      mark: mark(has('edu:') || has('education'),
                 f.education.length > 0 && f.education.every((ed) => ed.institution.trim())) },
  ]
  const otherGender = !GENDERS.some(([v]) => v === f.gender)

  return (
    <form className="page profile form-page" noValidate onSubmit={save}>
      <div className="page-head">
        <h1>Profile</h1>
        <p>
          <Icon d={P.lock} />
          <span>
            Stored only on this machine in <span className="mono">candidate_profile.toml</span>, never committed.
            The apply agent fills application forms from it.
          </span>
        </p>
      </div>

      {!exists && (
        <div className="alert am" role="status">
          <Icon d={P.refused} size={16} />
          <span>No profile saved yet — the apply agent can't fill contact details until you save one.</span>
        </div>
      )}
      {errors.form && (
        <div className="alert" role="alert">
          <Icon d={P.refused} size={16} />
          <span><b>Nothing was saved:</b> {errors.form}</span>
        </div>
      )}
      <ErrorSummary items={summarize(errors, f)} />

      <div className="form-layout">
        <SectionNav sections={sections} label="Profile sections" />
        <div className="form-sections">
          <section className="card" id="profile-contact">
            <div className="ch"><h2 className="ct">Contact</h2></div>
            <div className="grid2">
              <TextInput id="candidate_name" label="Full name" required value={f.candidate_name}
                         error={errors.candidate_name} onChange={(v) => set({ candidate_name: v })} />
              <TextInput id="candidate_email" label="Email" type="email" required value={f.candidate_email}
                         error={errors.candidate_email} onChange={(v) => set({ candidate_email: v })} />
              <TextInput id="candidate_phone" label="Phone" type="tel" required value={f.candidate_phone}
                         error={errors.candidate_phone} onChange={(v) => set({ candidate_phone: v })} />
              <TextInput id="linkedin_url" label="LinkedIn URL" type="url" mono value={f.linkedin_url ?? ''}
                         error={errors.linkedin_url} onChange={(v) => set({ linkedin_url: v || null })} />
              <TextInput id="portfolio_url" label="Portfolio URL" type="url" mono placeholder="Optional"
                         value={f.portfolio_url ?? ''} error={errors.portfolio_url}
                         onChange={(v) => set({ portfolio_url: v || null })} />
            </div>
          </section>

          <section className="card" id="profile-personal">
            <div className="ch"><h2 className="ct">Personal</h2></div>
            <div className="grid2">
              <Field id="gender" label="Gender" error={otherGender ? undefined : errors.gender}
                     help="Used only when a form asks. Decline to state is always allowed.">
                <select id="gender" className={inCls(otherGender ? undefined : errors.gender)}
                        value={otherGender ? 'other' : f.gender}
                        onChange={(e) => set({ gender: e.target.value === 'other' ? '' : e.target.value })}>
                  {GENDERS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                  <option value="other">Other</option>
                </select>
              </Field>
              {otherGender && (
                <TextInput id="gender_other" label="Please specify" value={f.gender} error={errors.gender}
                           onChange={(v) => set({ gender: v })} />
              )}
            </div>
          </section>

          <section className="card" id="profile-address">
            <div className="ch"><h2 className="ct">Address</h2></div>
            <div className="grid2">
              {ADDRESS.map(([k, label]) => (
                <TextInput key={k} id={`address_${k}`} label={label} span2={k === 'line1'} mono={k === 'postal_code'}
                           value={f.address[k]} error={errors[`address.${k}`]}
                           onChange={(v) => setAddress({ [k]: v })} />
              ))}
            </div>
          </section>

          <section className="card" id="profile-work">
            <div className="ch">
              <h2 className="ct">Work history</h2>
              <button type="button" className="btn sm" onClick={() => {
                const row = emptyWork()
                set({ work_history: [...f.work_history, row] })
                addOpen(row.id)
              }}><Icon d={P.plus} size={12} width={2.5} />Add work entry</button>
            </div>
            <div className="entries">
              {f.work_history.length === 0 && <p className="entries__none">No work history yet.</p>}
              {f.work_history.map((w, i) => {
                const err = (k: string) => errors[`work:${w.id}:${k}`]
                return (
                  <Entry key={w.id} open={open.has(w.id)} onToggle={() => toggle(w.id)}
                         heading={w.company || 'New work entry'}
                         summary={<Summary title={w.title || 'Untitled role'} meta={w.company}
                                           when={dates(w.start, w.end, w.current)} />}
                         first={i === 0} last={i === f.work_history.length - 1}
                         onMove={(d) => set({ work_history: move(f.work_history, i, d) })}
                         onRemove={() => set({ work_history: f.work_history.filter((_, j) => j !== i) })}>
                    <TextInput id={`work_${w.id}_company`} label="Company" required value={w.company}
                               error={err('company')} onChange={(v) => setWork(i, { company: v })} />
                    <TextInput id={`work_${w.id}_title`} label="Title" required value={w.title}
                               error={err('title')} onChange={(v) => setWork(i, { title: v })} />
                    <MonthInput id={`work_${w.id}_start`} label="Start" value={w.start} error={err('start')}
                                onChange={(v) => setWork(i, { start: v })} />
                    {w.current ? (
                      <TextInput id={`work_${w.id}_end`} label="End" value="Present" disabled onChange={() => {}} />
                    ) : (
                      <MonthInput id={`work_${w.id}_end`} label="End" value={w.end} error={err('end')}
                                  onChange={(v) => setWork(i, { end: v })} />
                    )}
                    <label className="f-check">
                      {/* Checking it clears End, so a stale date never reaches the apply agent. */}
                      <input type="checkbox" checked={w.current}
                             onChange={(e) => setWork(i, { current: e.target.checked,
                                                           end: e.target.checked ? '' : w.end })} />
                      I currently work here
                    </label>
                    <span />
                    <Field id={`work_${w.id}_description`} label="Description" span2 error={err('description')}>
                      <textarea id={`work_${w.id}_description`} className={inCls(err('description'))}
                                value={w.description} onChange={(e) => setWork(i, { description: e.target.value })} />
                    </Field>
                  </Entry>
                )
              })}
            </div>
          </section>

          <section className="card" id="profile-education">
            <div className="ch">
              <h2 className="ct">Education</h2>
              <button type="button" className="btn sm" onClick={() => {
                const row = emptyEdu()
                set({ education: [...f.education, row] })
                addOpen(row.id)
              }}><Icon d={P.plus} size={12} width={2.5} />Add education entry</button>
            </div>
            <div className="entries">
              {f.education.length === 0 && <p className="entries__none">No education yet.</p>}
              {f.education.map((ed, i) => {
                const err = (k: string) => errors[`edu:${ed.id}:${k}`]
                return (
                  <Entry key={ed.id} open={open.has(ed.id)} onToggle={() => toggle(ed.id)}
                         heading={ed.institution || 'New education entry'}
                         summary={<Summary title={[ed.degree, ed.field].filter(Boolean).join(', ') || 'Untitled'}
                                           meta={ed.institution} when={dates(ed.start, ed.end)} />}
                         first={i === 0} last={i === f.education.length - 1}
                         onMove={(d) => set({ education: move(f.education, i, d) })}
                         onRemove={() => set({ education: f.education.filter((_, j) => j !== i) })}>
                    <TextInput id={`edu_${ed.id}_institution`} label="Institution" required span2
                               value={ed.institution} error={err('institution')}
                               onChange={(v) => setEdu(i, { institution: v })} />
                    <TextInput id={`edu_${ed.id}_degree`} label="Degree" value={ed.degree} error={err('degree')}
                               onChange={(v) => setEdu(i, { degree: v })} />
                    <TextInput id={`edu_${ed.id}_field`} label="Field" value={ed.field} error={err('field')}
                               onChange={(v) => setEdu(i, { field: v })} />
                    <MonthInput id={`edu_${ed.id}_start`} label="Start" value={ed.start} error={err('start')}
                                onChange={(v) => setEdu(i, { start: v })} />
                    <MonthInput id={`edu_${ed.id}_end`} label="End" value={ed.end} error={err('end')}
                                onChange={(v) => setEdu(i, { end: v })} />
                  </Entry>
                )
              })}
            </div>
          </section>
        </div>
      </div>

      <SaveBar dirty={dirty} busy={saving} saveLabel="Save profile" onDiscard={() => {
        setForm(JSON.parse(snapshot) as ProfileForm)
        setErrors({})
        setOpen(new Set())
      }} />
    </form>
  )
}
