import { useEffect, useState } from 'react'
import { ApiError, errorText, get, post, put } from '../api'
import '../components/ui.css'

// The facts store the gate scores against and tailoring cites. Below
// min_hard facts, scoring and tailoring refuse to run at all.

interface Fact {
  id: number
  claim: string
  evidence: string
  project: string | null
  metric: string | null
  confidence: 'high' | 'medium' | 'low'
}

type Draft = Record<'claim' | 'evidence' | 'project' | 'metric' | 'confidence', string>

const EMPTY: Draft = { claim: '', evidence: '', project: '', metric: '', confidence: 'high' }

const toDraft = (f: Fact): Draft => ({
  claim: f.claim, evidence: f.evidence, project: f.project ?? '',
  metric: f.metric ?? '', confidence: f.confidence,
})

async function del(path: string): Promise<void> {
  const res = await fetch(path, { method: 'DELETE' })
  if (!res.ok) {
    const body = await res.json().catch(() => undefined)
    throw new ApiError(res.status, body?.message ?? `Request failed (${res.status})`, body)
  }
}

function Fields({ draft, setDraft, busy, idPrefix }: {
  draft: Draft; setDraft: (d: Draft) => void; busy: boolean; idPrefix: string
}) {
  const text = (name: keyof Draft, label: string) => (
    <div className="field-row">
      <label htmlFor={`${idPrefix}-${name}`}>{label}</label>
      <input id={`${idPrefix}-${name}`} value={draft[name]} disabled={busy}
            onChange={(e) => setDraft({ ...draft, [name]: e.target.value })} />
    </div>
  )
  return (
    <>
      {text('claim', 'Claim')}
      {text('evidence', 'Evidence')}
      {text('project', 'Project (optional)')}
      {text('metric', 'Metric (optional)')}
      <div className="field-row">
        <label htmlFor={`${idPrefix}-confidence`}>Confidence</label>
        <select id={`${idPrefix}-confidence`} value={draft.confidence} disabled={busy}
                onChange={(e) => setDraft({ ...draft, confidence: e.target.value })}>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>
      </div>
    </>
  )
}

function FactCard({ fact, onChanged }: { fact: Fact; onChanged: () => void }) {
  const [draft, setDraft] = useState(toDraft(fact))
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(fact))

  async function act(fn: () => Promise<unknown>) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <Fields draft={draft} setDraft={setDraft} busy={busy} idPrefix={`fact-${fact.id}`} />
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
        <button type="button" className="btn primary" disabled={busy || !dirty}
                onClick={() => act(() => put(`/api/facts/${fact.id}`, draft))}>
          Save
        </button>
        {confirmingDelete ? (
          <>
            <button type="button" className="btn" disabled={busy}
                    onClick={() => act(() => del(`/api/facts/${fact.id}`))}>
              Confirm delete
            </button>
            <button type="button" className="btn" disabled={busy}
                    onClick={() => setConfirmingDelete(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="btn" disabled={busy}
                  onClick={() => setConfirmingDelete(true)}>
            Delete
          </button>
        )}
      </div>
      {error && <div className="field-error">{error}</div>}
    </div>
  )
}

function AddFact({ onChanged }: { onChanged: () => void }) {
  const [draft, setDraft] = useState(EMPTY)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function add(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await post('/api/facts', draft)
      setDraft(EMPTY)
      onChanged()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={add}>
      <h2>Add a fact</h2>
      <Fields draft={draft} setDraft={setDraft} busy={busy} idPrefix="new-fact" />
      <button type="submit" className="btn primary"
              disabled={busy || !draft.claim.trim() || !draft.evidence.trim()}>
        Add fact
      </button>
      {error && <div className="field-error">{error}</div>}
    </form>
  )
}

export function Facts() {
  const [data, setData] = useState<{ items: Fact[]; min_hard: number; min_warn: number } | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  function load() {
    get<{ items: Fact[]; min_hard: number; min_warn: number }>('/api/facts')
      .then((r) => {
        setLoadError(null)
        setData(r)
      })
      .catch((err) => setLoadError(errorText(err)))
  }

  useEffect(load, [])

  if (loadError) {
    return (
      <>
        <h1 className="page-title">Facts</h1>
        <div className="banner banner--denied">Couldn't load facts: {loadError}</div>
      </>
    )
  }
  if (!data) return null
  const n = data.items.length

  return (
    <>
      <h1 className="page-title">Facts</h1>
      <p className="rationale">
        Verified claims about your work. The gate scores jobs against them and tailored
        resumes cite them, so only add what you can defend.
      </p>
      {n < data.min_hard ? (
        <div className="banner banner--denied">
          {n} facts — scoring and tailoring are blocked until you have at least {data.min_hard}.
        </div>
      ) : n < data.min_warn ? (
        <div className="banner banner--denied">
          {n} facts — scoring works, but {data.min_warn}+ gives more reliable verdicts.
        </div>
      ) : null}
      <AddFact onChanged={load} />
      {data.items.map((f) => (
        <FactCard key={f.id} fact={f} onChanged={load} />
      ))}
    </>
  )
}
