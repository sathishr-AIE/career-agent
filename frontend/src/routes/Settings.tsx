import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, get, put } from '../api'

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
  settings: { scoring_model: string; max_score_per_run: number }
  scoring_models: string[]
  model_labels: Record<string, string>
  saved: boolean
}

type FormState = Record<string, string | boolean>

const join = (xs: string[]) => xs.join(', ')

function formFromContext(ctx: SettingsContext): FormState {
  const b = ctx.brief
  return {
    target_titles: b ? join(b.target_titles) : '',
    title_families: b ? join(b.title_families) : '',
    search_locations: b ? join(b.search_locations) : '',
    locations: b ? join(b.locations) : '',
    work_authorization: b ? join(b.work_authorization) : '',
    excluded_companies: b ? join(b.excluded_companies) : '',
    non_negotiables: b ? join(b.non_negotiables) : '',
    remote_ok: b ? b.remote_ok : true,
    salary_floor_inr: b?.salary_floor_inr != null ? String(b.salary_floor_inr) : '',
    daily_cap: b ? String(b.daily_cap) : '',
    gate_threshold: b ? String(b.gate_threshold) : '',
    staleness_days: b ? String(b.staleness_days) : '',
    scoring_model: ctx.settings.scoring_model,
    max_score_per_run: String(ctx.settings.max_score_per_run),
  }
}

function Field({
  label,
  name,
  form,
  setForm,
  errors,
  hint,
  type = 'text',
}: {
  label: string
  name: string
  form: FormState
  setForm: (f: FormState) => void
  errors: Record<string, string>
  hint?: string
  type?: string
}) {
  return (
    <div className="field-row">
      <label htmlFor={name}>{label}</label>
      <input
        id={name}
        type={type}
        value={String(form[name] ?? '')}
        onChange={(e) => setForm({ ...form, [name]: e.target.value })}
      />
      {hint && <span className="hint">{hint}</span>}
      {errors[name] && <span className="field-error">{errors[name]}</span>}
    </div>
  )
}

export function Settings() {
  const [ctx, setCtx] = useState<SettingsContext | null>(null)
  const [form, setForm] = useState<FormState | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    get<SettingsContext>('/api/settings').then((c) => {
      setCtx(c)
      setForm(formFromContext(c))
    })
  }, [])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!form) return
    setSaving(true)
    setSaved(false)
    setErrors({})
    const body = {
      ...form,
      brief_present: !!ctx?.brief,
      // Contact details are edited on the Profile page now -- this never
      // carries candidate_* fields, so actions.save_settings leaves
      // candidate_profile.toml untouched (see its candidate_present docstring).
      candidate_present: false,
    }
    try {
      const next = await put<SettingsContext>('/api/settings', body)
      setCtx(next)
      setForm(formFromContext(next))
      setSaved(true)
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { errors?: Record<string, string> } | undefined
        setErrors(body?.errors ?? { form: err.message })
      }
    } finally {
      setSaving(false)
    }
  }

  if (!ctx || !form) return null

  return (
    <>
      <h1 className="page-title">Settings</h1>

      {saved && (
        <div className="banner banner--done">
          Settings saved. They take effect on the next run — no restart needed.
        </div>
      )}
      {Object.keys(errors).length > 0 && (
        <div className="banner banner--denied">
          <b>Nothing was saved.</b>
          <ul>
            {Object.entries(errors).map(([field, message]) => (
              <li key={field}>
                {field}: {message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {ctx.brief_error && (
        <div className="banner banner--denied">
          <b>Career brief unavailable.</b> {ctx.brief_error}
          <div className="rationale">
            Agent Settings below still work. The brief section is hidden rather than
            pre-filled with defaults, because saving those would overwrite the file with a
            brief you never chose.
          </div>
        </div>
      )}

      <form onSubmit={onSubmit}>
        {ctx.brief && (
          <div className="card">
            <h2>Career Brief</h2>
            <Field label="Target titles" name="target_titles" form={form} setForm={setForm}
                  errors={errors} hint="Comma-separated. At least one required." />
            <Field label="Title families" name="title_families" form={form} setForm={setForm}
                  errors={errors}
                  hint="Comma-separated. The hard filter matches these against job titles." />
            <Field label="Search locations" name="search_locations" form={form} setForm={setForm}
                  errors={errors}
                  hint="What we ASK sources for. Each city multiplies daily Actor runs." />
            <Field label="Accepted locations" name="locations" form={form} setForm={setForm}
                  errors={errors}
                  hint="What the hard filter ACCEPTS on the way back. Usually a superset." />
            <Field label="Work authorization" name="work_authorization" form={form} setForm={setForm} errors={errors} />
            <Field label="Excluded companies" name="excluded_companies" form={form} setForm={setForm} errors={errors} />
            <Field label="Non-negotiables" name="non_negotiables" form={form} setForm={setForm} errors={errors} />

            <div className="field-row">
              <label>
                <input
                  type="checkbox"
                  checked={!!form.remote_ok}
                  onChange={(e) => setForm({ ...form, remote_ok: e.target.checked })}
                />{' '}
                Remote acceptable
              </label>
            </div>

            <Field label="Salary floor (INR/year)" name="salary_floor_inr" form={form}
                  setForm={setForm} errors={errors} type="number"
                  hint="Leave blank for no floor. 0 would mean something else." />
            <Field label="Daily application cap" name="daily_cap" form={form} setForm={setForm}
                  errors={errors} type="number" />
            <Field label="Gate threshold" name="gate_threshold" form={form} setForm={setForm}
                  errors={errors} type="number"
                  hint='Weighted score at or above this scores "submit".' />
            <Field label="Staleness (days)" name="staleness_days" form={form} setForm={setForm}
                  errors={errors} type="number" />
          </div>
        )}

        <div className="card">
          <h2>Candidate Profile</h2>
          {ctx.candidate_error && <div className="banner banner--denied">{ctx.candidate_error}</div>}
          <p className="rationale">
            Name, contact details, work history and education are edited on the Profile page.
          </p>
          <Link className="btn" to="/profile">Open Profile →</Link>
        </div>

        <div className="card">
          <h2>Agent Settings</h2>
          <div className="field-row">
            <label htmlFor="scoring_model">Scoring model</label>
            <select
              id="scoring_model"
              value={String(form.scoring_model)}
              onChange={(e) => setForm({ ...form, scoring_model: e.target.value })}
            >
              {ctx.scoring_models.map((m) => (
                <option key={m} value={m}>
                  {ctx.model_labels[m]}
                </option>
              ))}
            </select>
            <span className="hint">Recorded on every verdict, so past scores stay attributable.</span>
            {errors.scoring_model && <span className="field-error">{errors.scoring_model}</span>}
          </div>
          <Field label="Jobs scored per run" name="max_score_per_run" form={form} setForm={setForm}
                errors={errors} type="number"
                hint="Caps model calls, not jobs examined. 0 runs discovery and the hard filter only." />
        </div>

        <button className="btn primary" type="submit" disabled={saving}>
          Save settings
        </button>
      </form>
    </>
  )
}
