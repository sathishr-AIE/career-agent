import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorText, get, put } from '../api'
import {
  ErrorSummary, FormSkeleton, LoadError, RadioCards, SaveBar, SectionNav, Slider, Stepper, Switch, TagInput,
  TextInput, useLeaveGuard, type NavSection,
} from '../components/FormPage'
import { useShell } from '../components/shell'
import './Settings.css'

// -- shapes /api/settings returns (context.settings_context) --

interface CareerBrief {
  target_titles: string[]
  title_families: string[]
  search_locations: string[]
  locations: string[]
  remote_ok: boolean
  salary_floor_inr: number | null
  work_authorization: string[]
  daily_cap: number
  gate_threshold: number
  staleness_days: number
  excluded_companies: string[]
  non_negotiables: string[]
}

interface SettingsContext {
  brief: CareerBrief | null
  brief_error: string | null
  candidate_error: string | null
  settings: { scoring_model: string; apply_model: string; max_score_per_run: number }
  scoring_models: string[]
  model_labels: Record<string, string>
  apply_models: string[]
  apply_model_labels: Record<string, string>
}

const LISTS = ['target_titles', 'title_families', 'search_locations', 'locations', 'work_authorization',
  'excluded_companies', 'non_negotiables'] as const
type ListKey = (typeof LISTS)[number]

interface SettingsForm extends Record<ListKey, string[]> {
  remote_ok: boolean
  // Numbers stay text as typed; the server parses them (actions._parse_int).
  salary_floor_inr: string
  daily_cap: string
  gate_threshold: string
  staleness_days: string
  scoring_model: string
  apply_model: string
  max_score_per_run: string
}

const toForm = (c: SettingsContext): SettingsForm => {
  const b = c.brief
  return {
    ...(Object.fromEntries(LISTS.map((k) => [k, b ? b[k] : []])) as Record<ListKey, string[]>),
    remote_ok: b ? b.remote_ok : true,
    salary_floor_inr: b?.salary_floor_inr != null ? String(b.salary_floor_inr) : '',
    daily_cap: b ? String(b.daily_cap) : '',
    gate_threshold: b ? String(b.gate_threshold) : '',
    staleness_days: b ? String(b.staleness_days) : '',
    scoring_model: c.settings.scoring_model,
    apply_model: c.settings.apply_model,
    max_score_per_run: String(c.settings.max_score_per_run),
  }
}

// Every field in page order: its human label and the section it sits in.
const FIELDS: [keyof SettingsForm, string, string][] = [
  ['target_titles', 'Target titles', 'settings-search'],
  ['title_families', 'Title families', 'settings-search'],
  ['search_locations', 'Search locations', 'settings-search'],
  ['locations', 'Accepted locations', 'settings-search'],
  ['remote_ok', 'Remote acceptable', 'settings-search'],
  ['work_authorization', 'Work authorization', 'settings-filters'],
  ['salary_floor_inr', 'Salary floor', 'settings-filters'],
  ['excluded_companies', 'Excluded companies', 'settings-filters'],
  ['non_negotiables', 'Non-negotiables', 'settings-filters'],
  ['staleness_days', 'Staleness', 'settings-filters'],
  ['daily_cap', 'Daily application cap', 'settings-pacing'],
  ['gate_threshold', 'Gate threshold', 'settings-pacing'],
  ['scoring_model', 'Scoring model', 'settings-models'],
  ['apply_model', 'Apply agent model', 'settings-models'],
  ['max_score_per_run', 'Jobs scored per run', 'settings-models'],
]

function Icon({ d, size = 14 }: { d: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

const INFO = 'M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18zM12 7.5v5M12 16v.5'

/** Settings (approved Screen 10): how the agent searches, filters, scores and
 * paces itself. PUT sends the whole form; the server validates everything
 * before writing anything, and the brief is written back comment-preserving. */
export function Settings() {
  const { toast } = useShell()
  const [ctx, setCtx] = useState<SettingsContext | null>(null)
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [snapshot, setSnapshot] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)

  const dirty = form !== null && JSON.stringify(form) !== snapshot
  useLeaveGuard(dirty)

  const reset = (c: SettingsContext) => {
    const f = toForm(c)
    setCtx(c)
    setForm(f)
    setSnapshot(JSON.stringify(f))
    setErrors({})
  }

  const load = useCallback(() => {
    get<SettingsContext>('/api/settings')
      .then((c) => {
        reset(c)
        setLoadError(null)
      })
      .catch((e: unknown) => setLoadError(errorText(e)))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (Object.keys(errors).length) {
      document.querySelector('.settings .bad')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [errors])

  if (!ctx || !form) {
    return loadError
      ? <LoadError title="Settings" what="settings" error={loadError} onRetry={load} />
      : <FormSkeleton title="Settings" />
  }

  const f = form
  const set = (patch: Partial<SettingsForm>) => setForm({ ...f, ...patch })

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!dirty || saving || !ctx) return
    setSaving(true)
    const body = {
      ...f,
      // The API's list shape is comma-joined text; a chip never holds a comma.
      ...Object.fromEntries(LISTS.map((k) => [k, f[k].join(', ')])),
      brief_present: !!ctx.brief,
      // Contact details live on Profile: without candidate_* fields,
      // actions.save_settings leaves candidate_profile.toml untouched.
      candidate_present: false,
    }
    try {
      reset(await put<SettingsContext>('/api/settings', body))
      toast({ text: 'Settings saved — they take effect on the next run' })
    } catch (err) {
      const b = err instanceof ApiError ? (err.body as { errors?: Record<string, string> } | undefined) : undefined
      setErrors(b?.errors ?? { form: errorText(err) })
    } finally {
      setSaving(false)
    }
  }

  const bad = (section: string) => FIELDS.some(([k, , s]) => s === section && errors[k])
  const mark = (section: string): NavSection['mark'] => (bad(section) ? 'bad' : undefined)
  const sections: NavSection[] = [
    ...(ctx.brief
      ? [
          { id: 'settings-search', label: 'Search', mark: mark('settings-search') },
          { id: 'settings-filters', label: 'Filters & eligibility', mark: mark('settings-filters') },
          { id: 'settings-pacing', label: 'Apply pacing & gate', mark: mark('settings-pacing') },
        ]
      : [{ id: 'settings-brief', label: 'Career brief', mark: 'bad' as const }]),
    { id: 'settings-models', label: 'Models & budget', mark: mark('settings-models') },
    { id: 'settings-candidate', label: 'Candidate details', mark: ctx.candidate_error ? 'bad' : undefined },
  ]
  const summary = [
    ...FIELDS.filter(([k]) => errors[k]).map(([k, label]) => ({ label, target: k })),
    ...Object.keys(errors)
      .filter((k) => k !== 'form' && !FIELDS.some(([f]) => f === k))
      .map((k) => ({ label: k })),
  ]
  const list = (k: ListKey, label: string, extra: { required?: boolean; help?: string; placeholder?: string } = {}) => (
    <TagInput id={k} label={label} value={f[k]} error={errors[k]} onChange={(v) => set({ [k]: v })} {...extra} />
  )

  return (
    <form className="page settings form-page" noValidate onSubmit={save}>
      <div className="page-head">
        <h1>Settings</h1>
        <p>How the agent searches, filters, scores and paces itself. Changes take effect on the next run.</p>
      </div>

      {errors.form && (
        <div className="alert" role="alert">
          <Icon d={INFO} size={16} />
          <span><b>Nothing was saved:</b> {errors.form}</span>
        </div>
      )}
      <ErrorSummary items={summary} />

      <div className="form-layout">
        <SectionNav sections={sections} label="Settings sections" />
        <div className="form-sections">
          {ctx.brief ? (
            <>
              <section className="card" id="settings-search">
                <div className="ch"><h2 className="ct">Search</h2></div>
                <div className="set-body">
                  <div className="grid2">
                    {list('target_titles', 'Target titles', { required: true, placeholder: 'Add a title' })}
                    {list('title_families', 'Title families',
                          { help: 'The hard filter matches these against job titles.' })}
                    {list('search_locations', 'Search locations', {
                      required: true, placeholder: 'Add a location',
                      help: 'What we ask sources for — each city multiplies daily Actor runs.' })}
                    {list('locations', 'Accepted locations', {
                      placeholder: 'Add a location',
                      help: 'What the hard filter accepts on the way back — usually a superset.' })}
                  </div>
                  <Switch label="Remote acceptable" checked={f.remote_ok} onChange={(v) => set({ remote_ok: v })} />
                </div>
              </section>

              <section className="card" id="settings-filters">
                <div className="ch"><h2 className="ct">Filters &amp; eligibility</h2></div>
                <div className="set-body">
                  <div className="grid2">
                    {list('work_authorization', 'Work authorization', { placeholder: 'Add a country' })}
                    <TextInput id="salary_floor_inr" label="Salary floor (INR / year)" type="number" min={0} mono
                               value={f.salary_floor_inr} error={errors.salary_floor_inr}
                               help="Leave blank for no floor. 0 would mean something else."
                               onChange={(v) => set({ salary_floor_inr: v })} />
                    {list('excluded_companies', 'Excluded companies', { placeholder: 'Add a company' })}
                    {list('non_negotiables', 'Non-negotiables', { placeholder: 'Add a non-negotiable' })}
                    <TextInput id="staleness_days" label="Staleness (days)" type="number" min={1} mono short
                               value={f.staleness_days} error={errors.staleness_days}
                               help="Postings older than this are ignored."
                               onChange={(v) => set({ staleness_days: v })} />
                  </div>
                </div>
              </section>

              <section className="card" id="settings-pacing">
                <div className="ch"><h2 className="ct">Apply pacing &amp; gate</h2></div>
                <div className="set-body">
                  <div className="grid2">
                    <Stepper id="daily_cap" label="Daily application cap" min={1} value={f.daily_cap}
                             error={errors.daily_cap} onChange={(v) => set({ daily_cap: v })}
                             help="At least 1. The worker stops for the day at this many submissions." />
                    <Slider id="gate_threshold" label="Gate threshold" value={f.gate_threshold}
                            error={errors.gate_threshold} onChange={(v) => set({ gate_threshold: v })}
                            help='Weighted score at or above this scores "submit".' />
                  </div>
                </div>
              </section>
            </>
          ) : (
            <section className="alert settings__broken" id="settings-brief" role="alert">
              <Icon d={INFO} size={16} />
              <span>
                <b>Career brief unavailable:</b> {ctx.brief_error}
                <span className="settings__why">
                  These fields are hidden rather than filled with defaults, which would overwrite your file
                  with a brief you never chose. Models &amp; budget still save.
                </span>
              </span>
            </section>
          )}

          <section className="card" id="settings-models">
            <div className="ch">
              <h2 className="ct">Models &amp; budget</h2>
              <span className="help">The chat composer's model picker changes these same two settings.</span>
            </div>
            <div className="set-body">
              <RadioCards name="scoring_model" label="Scoring model" options={ctx.scoring_models}
                          labels={ctx.model_labels} value={f.scoring_model} error={errors.scoring_model}
                          onChange={(v) => set({ scoring_model: v })}
                          help="Recorded on every verdict, so past scores stay attributable." />
              <RadioCards name="apply_model" label="Apply agent model" options={ctx.apply_models}
                          labels={ctx.apply_model_labels} value={f.apply_model} error={errors.apply_model}
                          onChange={(v) => set({ apply_model: v })}
                          help="Used for the next Apply or Continue; a running session keeps its model." />
              <TextInput id="max_score_per_run" label="Jobs scored per run" type="number" min={0} mono short
                         value={f.max_score_per_run} error={errors.max_score_per_run}
                         help="Caps model calls, not jobs examined. 0 runs discovery and the hard filter only."
                         onChange={(v) => set({ max_score_per_run: v })} />
            </div>
          </section>

          <section className="card settings__candidate" id="settings-candidate">
            <div className="settings__candidate-row">
              <div className="settings__candidate-text">
                <h2 className="ct">Candidate details</h2>
                <span>Name, contact details, address, work history and education live on Profile.</span>
              </div>
              <Link className="btn" to="/profile">
                Open Profile
                <Icon d="M5 12h14M13 6l6 6-6 6" />
              </Link>
            </div>
            {ctx.candidate_error && (
              <div className="alert" role="alert">
                <Icon d={INFO} size={16} />
                <span><b>Your profile file can't be read:</b> {ctx.candidate_error}</span>
              </div>
            )}
          </section>
        </div>
      </div>

      <SaveBar dirty={dirty} busy={saving} saveLabel="Save settings" onDiscard={() => {
        setForm(JSON.parse(snapshot) as SettingsForm)
        setErrors({})
      }} />
    </form>
  )
}
