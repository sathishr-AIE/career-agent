import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ApiError, del, errorText, get, post, put } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Field, Segmented, TextInput, inCls } from '../components/FormPage'
import { Icon } from '../components/Icon'
import { Menu } from '../components/Menu'
import { TopBarActions, useShell } from '../components/shell'
import { P } from '../icons'
import './Facts.css'

// -- shapes /api/facts returns (web/api_facts.py; FC1 adds cited_by) --

type Confidence = 'high' | 'medium' | 'low'

interface Fact {
  id: number
  claim: string
  evidence: string
  project: string | null
  metric: string | null
  confidence: Confidence
  /** Tailored resume versions whose bullets cite this fact. */
  cited_by: string[]
}

interface FactsResponse {
  items: Fact[]
  /** Scoring and tailoring refuse to run below this many facts (gate.MIN_FACTS_HARD). */
  min_hard: number
  min_warn: number
}

type Draft = Record<'claim' | 'evidence' | 'project' | 'metric', string> & { confidence: Confidence }

const EMPTY: Draft = { claim: '', evidence: '', project: '', metric: '', confidence: 'high' }
const toDraft = (f: Fact): Draft => ({
  claim: f.claim, evidence: f.evidence, project: f.project ?? '', metric: f.metric ?? '',
  confidence: f.confidence,
})

const CONFIDENCE: [Confidence, string, string][] = [
  ['high', 'High', 'em'], ['medium', 'Medium', 'am'], ['low', 'Low', 'sl'],
]
const LEVELS = CONFIDENCE.map(([v, label]) => [v, label] as [string, string])

/** The add panel, and the same fields in place inside a card while editing. */
function FactForm({ draft, setDraft, errors, busy, cited, onSave, onCancel }: {
  draft: Draft
  setDraft: (d: Draft) => void
  errors: Record<string, string>
  busy: boolean
  cited: string[]
  onSave: () => void
  onCancel: () => void
}) {
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch })
  const ready = draft.claim.trim() && draft.evidence.trim()
  return (
    <section className="card fact-form">
      <div className="fact-form__body">
        <TextInput id="fact-claim" label="Claim" required value={draft.claim} error={errors.claim}
                   onChange={(v) => set({ claim: v })} />
        <Field id="fact-evidence" label="Evidence" required error={errors.evidence}
               help="What you shipped and the measurable result.">
          <textarea id="fact-evidence" className={inCls(errors.evidence)} value={draft.evidence}
                    onChange={(e) => set({ evidence: e.target.value })} />
        </Field>
        <div className="fact-form__row">
          <TextInput id="fact-project" label="Project" value={draft.project}
                     onChange={(v) => set({ project: v })} />
          <TextInput id="fact-metric" label="Metric" mono value={draft.metric}
                     onChange={(v) => set({ metric: v })} />
          <Segmented id="fact-confidence" label="Confidence" options={LEVELS} value={draft.confidence}
                     error={errors.confidence} onChange={(v) => set({ confidence: v as Confidence })} />
        </div>
        {cited.length > 0 && (
          <div className="fact-form__cited">
            Resumes <span className="mono">{cited.join(', ')}</span> cite this fact — your edit changes what they show.
          </div>
        )}
        {errors.form && <div className="err">{errors.form}</div>}
        <div className="fact-form__actions">
          <button type="button" className="btn pri" disabled={!ready || busy} onClick={onSave}>Save</button>
          <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </section>
  )
}

function FactCard({ f, error, onEdit, onDelete }: {
  f: Fact
  error?: string
  onEdit: () => void
  onDelete: () => void
}) {
  const cited = f.cited_by.length > 0
  const [, label, tone] = CONFIDENCE.find(([v]) => v === f.confidence) ?? CONFIDENCE[0]
  const why = cited ? `Cited by ${f.cited_by.join(', ')} — edit it instead` : undefined
  return (
    <section className="card fact-card" id={`fact-${f.id}`}>
      <div className="fact-card__text">
        <span className="fact-card__claim">{f.claim}</span>
        <span className="fact-card__evidence">{f.evidence}</span>
        <span className="fact-card__meta">
          {f.project && <span className="meta">Project · {f.project}</span>}
          {f.metric && <span className="meta mono">{f.metric}</span>}
          {cited && (
            <Link className="meta meta--link" to="/resumes">
              Cited by <span className="mono">{f.cited_by.join(', ')}</span>
            </Link>
          )}
        </span>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="fact-card__side">
        <span className={`b ${tone}`}>{label}</span>
        <span className="fact-card__actions">
          <button type="button" className="btn sm" onClick={onEdit}>Edit</button>
          <button type="button" className="btn sm ghost rose-text" disabled={cited} title={why}
                  onClick={onDelete}>Delete</button>
        </span>
        <span className="fact-card__menu">
          <Menu items={[
            { label: 'Edit', icon: P.file, onSelect: onEdit },
            { label: 'Delete', icon: P.trash, danger: true, disabled: cited, onSelect: onDelete },
          ]} note={why} />
        </span>
        {cited && <span className="fact-card__note">Cited — edit instead of deleting</span>}
      </div>
    </section>
  )
}

/** Facts (approved Screen 7): the verified claims the gate scores credibility
 * against and tailored resumes cite. Below min_hard, scoring and tailoring
 * refuse to run at all, which is what the coverage card is for. */
export function Facts() {
  const { toast } = useShell()
  const { hash } = useLocation()
  const [data, setData] = useState<FactsResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [doomed, setDoomed] = useState<Fact | null>(null)
  const [rowError, setRowError] = useState<Record<number, string>>({})
  const [level, setLevel] = useState<'all' | Confidence>('all')
  const [citedOnly, setCitedOnly] = useState(false)
  const [filter, setFilter] = useState('')

  const load = useCallback(
    () =>
      get<FactsResponse>('/api/facts')
        .then((d) => {
          setData(d)
          setLoadError(null)
        })
        .catch((e: unknown) => setLoadError(errorText(e))),
    [],
  )

  useEffect(() => {
    load()
  }, [load])

  // A citation chip on Resumes links to /facts#fact-12.
  useEffect(() => {
    if (!data || !hash.startsWith('#fact-')) return
    const el = document.getElementById(hash.slice(1))
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el?.classList.add('is-target')
    const t = setTimeout(() => el?.classList.remove('is-target'), 2000)
    return () => clearTimeout(t)
  }, [data, hash])

  if (!data) {
    return (
      <div className="page facts">
        <div className="page-head"><h1>Facts</h1></div>
        {loadError ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load your facts:</b> {loadError}</span>
            <button type="button" className="btn facts__retry" onClick={load}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="facts__skel">
            {[0, 1, 2].map((i) => (
              <div className="card facts__skel-card" key={i}>
                {[50, 85].map((w) => <span className="skel" key={w} style={{ width: `${w}%` }} />)}
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  const { items, min_hard: hard, min_warn: warn } = data
  const count = items.length
  const tone = count < hard ? 'ro' : count < warn ? 'am' : 'em'
  const short = hard - count
  const more = warn - count
  const message =
    count < hard
      ? `Scoring and tailoring are blocked — add ${short} more ${short === 1 ? 'fact' : 'facts'}.`
      : count < warn
        ? `Scoring works — ${more} more ${more === 1 ? 'fact gives' : 'facts give'} more reliable verdicts.`
        : 'Enough facts for reliable scoring.'
  // The bar runs a fifth past min_warn, so both marks sit inside it.
  const scale = Math.max(warn * 1.2, count)
  const pct = (n: number) => `${Math.min(100, (n / scale) * 100)}%`

  const q = filter.trim().toLowerCase()
  const shown = items.filter(
    (f) =>
      (level === 'all' || f.confidence === level) &&
      (!citedOnly || f.cited_by.length > 0) &&
      (!q || `${f.claim} ${f.project ?? ''}`.toLowerCase().includes(q)),
  )

  function edit(f: Fact) {
    setEditing(f.id)
    setDraft(toDraft(f))
    setErrors({})
  }

  function add() {
    setEditing('new')
    setDraft(EMPTY)
    setErrors({})
  }

  async function save() {
    if (busy || editing === null) return
    setBusy(true)
    setErrors({})
    const body = {
      ...draft,
      project: draft.project.trim() || null,
      metric: draft.metric.trim() || null,
    }
    try {
      if (editing === 'new') await post('/api/facts', body)
      else await put(`/api/facts/${editing}`, body)
      await load()
      setEditing(null)
      toast({ text: editing === 'new' ? 'Fact added' : 'Fact saved' })
    } catch (err) {
      const b = err instanceof ApiError ? (err.body as { errors?: Record<string, string> } | undefined) : undefined
      setErrors(b?.errors ?? { form: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  async function remove(f: Fact) {
    setBusy(true)
    try {
      await del(`/api/facts/${f.id}`)
      await load()
      setDoomed(null)
      toast({ text: 'Fact deleted' })
    } catch (err) {
      // 409: a tailored resume cites it. Delete is already disabled for that,
      // so this is the race where a resume cited it meanwhile.
      setDoomed(null)
      setRowError({ [f.id]: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page facts">
      <TopBarActions>
        <button type="button" className="btn pri" onClick={add}>
          <Icon d={P.plus} width={2.5} />Add fact
        </button>
      </TopBarActions>

      <div className="page-head">
        <h1>Facts</h1>
        <p>Verified claims about your work. Only add what you can defend — resumes cite these.</p>
      </div>

      <section className="card coverage">
        <div className="coverage__count">
          <span className="mono coverage__n">{count}</span>
          <span className="help">{count === 1 ? 'fact' : 'facts'}</span>
        </div>
        <div className="coverage__bar">
          <div className="coverage__track">
            <div className={`coverage__fill coverage__fill--${tone}`} style={{ width: pct(count) }} />
            {[hard, warn].map((mark) => (
              <span className="coverage__tick" key={mark} style={{ left: pct(mark) }} />
            ))}
          </div>
          <div className="coverage__marks">
            <span className="mono" style={{ left: pct(hard) }}>{hard} · scoring</span>
            <span className="mono" style={{ left: pct(warn) }}>{warn} · reliable</span>
          </div>
        </div>
        <div className={`coverage__msg coverage__msg--${tone}`}>
          <Icon d={tone === 'em' ? P.allClear : P.refused} size={16} />
          <span>{message}</span>
        </div>
      </section>

      {items.length > 0 && (
        <div className="facts__filters">
          <div className="facts__chips">
            <button type="button" className="chip" aria-pressed={level === 'all'} onClick={() => setLevel('all')}>
              All<span className="mono">{count}</span>
            </button>
            {CONFIDENCE.map(([value, label]) => (
              <button key={value} type="button" className="chip" aria-pressed={level === value}
                      onClick={() => setLevel(value)}>
                {label}<span className="mono">{items.filter((f) => f.confidence === value).length}</span>
              </button>
            ))}
            <span className="facts__sep" />
            <button type="button" className="chip" aria-pressed={citedOnly} onClick={() => setCitedOnly((c) => !c)}>
              <span className={citedOnly ? 'mini-switch mini-switch--on' : 'mini-switch'} />Cited only
            </button>
          </div>
          <span className="searchbox">
            <Icon d={P.search} />
            <input className="field facts__filter" value={filter} onChange={(e) => setFilter(e.target.value)}
                   placeholder="Filter claims and projects" aria-label="Filter facts" />
          </span>
        </div>
      )}

      {editing === 'new' && (
        <FactForm draft={draft} setDraft={setDraft} errors={errors} busy={busy} cited={[]}
                  onSave={save} onCancel={() => setEditing(null)} />
      )}

      {items.length === 0 ? (
        <div className="card empty">
          <span className="empty__icon"><Icon d={P.check} size={16} /></span>
          No facts yet
          <span className="help">Scoring needs at least {hard}.</span>
          <button type="button" className="btn" onClick={add}><Icon d={P.plus} width={2.5} />Add fact</button>
        </div>
      ) : shown.length === 0 ? (
        <div className="card empty">
          No facts match these filters
          <button type="button" className="btn" onClick={() => {
            setLevel('all')
            setCitedOnly(false)
            setFilter('')
          }}>Clear filters</button>
        </div>
      ) : (
        shown.map((f) =>
          editing === f.id ? (
            <FactForm key={f.id} draft={draft} setDraft={setDraft} errors={errors} busy={busy} cited={f.cited_by}
                      onSave={save} onCancel={() => setEditing(null)} />
          ) : (
            <FactCard key={f.id} f={f} error={rowError[f.id]} onEdit={() => edit(f)}
                      onDelete={() => setDoomed(f)} />
          ),
        )
      )}

      {doomed && (
        <ConfirmDialog
          title="Delete this fact?"
          text={count - 1 < hard
            ? `You'll have ${count - 1} facts — scoring and tailoring will be blocked.`
            : 'This removes the claim and its evidence. It cannot be undone.'}
          confirm="Delete" busy={busy}
          onConfirm={() => remove(doomed)} onCancel={() => setDoomed(null)} />
      )}
    </div>
  )
}
