import { useEffect, useState } from 'react'
import { ApiError, get, put } from '../api'
import '../components/ui.css'

// Types declared locally per Task 17's controller ruling -- api.ts is not
// touched here to avoid colliding with the parallel task editing it.

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

interface ProfileResponse {
  exists: boolean
  profile: CandidateProfile
}

const GENDER_OPTIONS = ['decline', 'female', 'male', 'non-binary'] as const

const emptyWork = (): WorkEntry => ({
  company: '', title: '', start: '', end: '', current: false, description: '',
})

const emptyEdu = (): EduEntry => ({
  institution: '', degree: '', field: '', start: '', end: '',
})

function move<T>(list: T[], index: number, delta: number): T[] {
  const target = index + delta
  if (target < 0 || target >= list.length) return list
  const next = [...list]
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}

function TextField({
  label, id, value, onChange, error, type = 'text', disabled = false,
}: {
  label: string
  id: string
  value: string
  onChange: (v: string) => void
  error?: string
  type?: string
  disabled?: boolean
}) {
  return (
    <div className="field-row">
      <label htmlFor={id}>{label}</label>
      <input id={id} type={type} value={value} disabled={disabled}
            onChange={(e) => onChange(e.target.value)} />
      {error && <span className="field-error">{error}</span>}
    </div>
  )
}

export function Profile() {
  const [exists, setExists] = useState(false)
  const [profile, setProfile] = useState<CandidateProfile | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    get<ProfileResponse>('/api/profile').then((r) => {
      setExists(r.exists)
      setProfile(r.profile)
    })
  }, [])

  if (!profile) return null

  const isOtherGender = !GENDER_OPTIONS.includes(profile.gender as (typeof GENDER_OPTIONS)[number])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!profile) return
    setSaving(true)
    setSaved(false)
    setErrors({})
    try {
      await put<{ ok: boolean; message: string }>('/api/profile', profile)
      setExists(true)
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

  return (
    <>
      <h1 className="page-title">Profile</h1>
      {!exists && (
        <div className="banner">
          No profile saved yet. Fill it in and Save -- the apply agent fills real
          applications from this.
        </div>
      )}
      {saved && <div className="banner banner--done">Profile saved.</div>}
      {errors.form && <div className="banner banner--denied">{errors.form}</div>}

      <form onSubmit={onSubmit}>
        <div className="card">
          <h2>Contact</h2>
          <TextField label="Full name" id="candidate_name" value={profile.candidate_name}
                    error={errors.candidate_name}
                    onChange={(v) => setProfile({ ...profile, candidate_name: v })} />
          <TextField label="Email" id="candidate_email" value={profile.candidate_email}
                    error={errors.candidate_email}
                    onChange={(v) => setProfile({ ...profile, candidate_email: v })} />
          <TextField label="Phone" id="candidate_phone" value={profile.candidate_phone}
                    error={errors.candidate_phone}
                    onChange={(v) => setProfile({ ...profile, candidate_phone: v })} />
          <TextField label="LinkedIn URL" id="linkedin_url" value={profile.linkedin_url ?? ''}
                    error={errors.linkedin_url}
                    onChange={(v) => setProfile({ ...profile, linkedin_url: v || null })} />
          <TextField label="Portfolio URL" id="portfolio_url" value={profile.portfolio_url ?? ''}
                    error={errors.portfolio_url}
                    onChange={(v) => setProfile({ ...profile, portfolio_url: v || null })} />
        </div>

        <div className="card">
          <h2>Gender</h2>
          <div className="field-row">
            <label htmlFor="gender">Gender</label>
            <select
              id="gender"
              value={isOtherGender ? 'other' : profile.gender}
              onChange={(e) => setProfile({
                ...profile,
                gender: e.target.value === 'other' ? '' : e.target.value,
              })}
            >
              <option value="decline">Decline to state</option>
              <option value="female">Female</option>
              <option value="male">Male</option>
              <option value="non-binary">Non-binary</option>
              <option value="other">Other</option>
            </select>
          </div>
          {isOtherGender && (
            <TextField label="Please specify" id="gender_other" value={profile.gender}
                      error={errors.gender}
                      onChange={(v) => setProfile({ ...profile, gender: v })} />
          )}
        </div>

        <div className="card">
          <h2>Address</h2>
          <TextField label="Address line 1" id="address_line1" value={profile.address.line1}
                    error={errors['address.line1']}
                    onChange={(v) => setProfile({ ...profile, address: { ...profile.address, line1: v } })} />
          <TextField label="City" id="address_city" value={profile.address.city}
                    error={errors['address.city']}
                    onChange={(v) => setProfile({ ...profile, address: { ...profile.address, city: v } })} />
          <TextField label="State" id="address_state" value={profile.address.state}
                    error={errors['address.state']}
                    onChange={(v) => setProfile({ ...profile, address: { ...profile.address, state: v } })} />
          <TextField label="Postal code" id="address_postal_code" value={profile.address.postal_code}
                    error={errors['address.postal_code']}
                    onChange={(v) => setProfile({ ...profile, address: { ...profile.address, postal_code: v } })} />
          <TextField label="Country" id="address_country" value={profile.address.country}
                    error={errors['address.country']}
                    onChange={(v) => setProfile({ ...profile, address: { ...profile.address, country: v } })} />
        </div>

        <div className="card">
          <h2>Work history</h2>
          {profile.work_history.map((w, i) => {
            const set = (patch: Partial<WorkEntry>) => {
              const next = [...profile.work_history]
              next[i] = { ...next[i], ...patch }
              setProfile({ ...profile, work_history: next })
            }
            return (
              <div key={i} style={{ borderTop: i > 0 ? '1px solid var(--paper-line)' : undefined,
                                    paddingTop: i > 0 ? 'var(--space-4)' : undefined,
                                    marginTop: i > 0 ? 'var(--space-4)' : undefined }}>
                <TextField label="Company" id={`work_${i}_company`} value={w.company}
                          error={errors[`work_history.${i}.company`]}
                          onChange={(v) => set({ company: v })} />
                <TextField label="Title" id={`work_${i}_title`} value={w.title}
                          error={errors[`work_history.${i}.title`]}
                          onChange={(v) => set({ title: v })} />
                <TextField label="Start (YYYY-MM)" id={`work_${i}_start`} value={w.start}
                          error={errors[`work_history.${i}.start`]}
                          onChange={(v) => set({ start: v })} />
                <div className="field-row">
                  <label htmlFor={`work_${i}_current`}>
                    <input id={`work_${i}_current`} type="checkbox" checked={w.current}
                          onChange={(e) => set({ current: e.target.checked })} />{' '}
                    I currently work here
                  </label>
                </div>
                <TextField label="End (YYYY-MM)" id={`work_${i}_end`} value={w.end}
                          error={errors[`work_history.${i}.end`]} disabled={w.current}
                          onChange={(v) => set({ end: v })} />
                <div className="field-row">
                  <label htmlFor={`work_${i}_description`}>Description</label>
                  <textarea id={`work_${i}_description`} value={w.description}
                           onChange={(e) => set({ description: e.target.value })} />
                  {errors[`work_history.${i}.description`] &&
                    <span className="field-error">{errors[`work_history.${i}.description`]}</span>}
                </div>
                <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-4)' }}>
                  <button type="button" className="btn" disabled={i === 0}
                         onClick={() => setProfile({ ...profile, work_history: move(profile.work_history, i, -1) })}>
                    Move up
                  </button>
                  <button type="button" className="btn" disabled={i === profile.work_history.length - 1}
                         onClick={() => setProfile({ ...profile, work_history: move(profile.work_history, i, 1) })}>
                    Move down
                  </button>
                  <button type="button" className="btn"
                         onClick={() => setProfile({
                           ...profile,
                           work_history: profile.work_history.filter((_, j) => j !== i),
                         })}>
                    Remove
                  </button>
                </div>
              </div>
            )
          })}
          <button type="button" className="btn"
                 onClick={() => setProfile({ ...profile, work_history: [...profile.work_history, emptyWork()] })}>
            Add work entry
          </button>
        </div>

        <div className="card">
          <h2>Education</h2>
          {profile.education.map((ed, i) => {
            const set = (patch: Partial<EduEntry>) => {
              const next = [...profile.education]
              next[i] = { ...next[i], ...patch }
              setProfile({ ...profile, education: next })
            }
            return (
              <div key={i} style={{ borderTop: i > 0 ? '1px solid var(--paper-line)' : undefined,
                                    paddingTop: i > 0 ? 'var(--space-4)' : undefined,
                                    marginTop: i > 0 ? 'var(--space-4)' : undefined }}>
                <TextField label="Institution" id={`edu_${i}_institution`} value={ed.institution}
                          error={errors[`education.${i}.institution`]}
                          onChange={(v) => set({ institution: v })} />
                <TextField label="Degree" id={`edu_${i}_degree`} value={ed.degree}
                          error={errors[`education.${i}.degree`]}
                          onChange={(v) => set({ degree: v })} />
                <TextField label="Field" id={`edu_${i}_field`} value={ed.field}
                          error={errors[`education.${i}.field`]}
                          onChange={(v) => set({ field: v })} />
                <TextField label="Start" id={`edu_${i}_start`} value={ed.start}
                          error={errors[`education.${i}.start`]}
                          onChange={(v) => set({ start: v })} />
                <TextField label="End" id={`edu_${i}_end`} value={ed.end}
                          error={errors[`education.${i}.end`]}
                          onChange={(v) => set({ end: v })} />
                <button type="button" className="btn"
                       onClick={() => setProfile({
                         ...profile,
                         education: profile.education.filter((_, j) => j !== i),
                       })}>
                  Remove
                </button>
              </div>
            )
          })}
          <button type="button" className="btn"
                 onClick={() => setProfile({ ...profile, education: [...profile.education, emptyEdu()] })}
                 style={{ marginTop: profile.education.length > 0 ? 'var(--space-4)' : undefined }}>
            Add education entry
          </button>
        </div>

        <button className="btn primary" type="submit" disabled={saving}>
          {saving ? 'Saving...' : 'Save profile'}
        </button>
      </form>
    </>
  )
}
