import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, del, errorText, post, put } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Icon, Spinner } from '../components/Icon'
import { Menu } from '../components/Menu'
import { TopBarActions, useShell } from '../components/shell'
import { usePoll } from '../components/usePoll'
import { P } from '../icons'
import './Facts.css'

// The claims the gate scores credibility against and tailoring cites. Below
// min_hard facts, scoring and tailoring refuse to run at all.

type Confidence = 'high' | 'medium' | 'low'

interface Fact {
  id: number
  claim: string
  evidence: string
  project: string | null
  metric: string | null
  confidence: Confidence
  /** FC1: resume versions whose bullets cite this fact. */
  cited_by: string[]
}

interface FactsData {
  items: Fact[]
  min_hard: number
  min_warn: number
}

type Draft = Record<'claim' | 'evidence' | 'project' | 'metric' | 'confidence', string>

const EMPTY: Draft = { claim: '', evidence: '', project: '', metric: '', confidence: 'high' }

const toDraft = (f: Fact): Draft => ({
  claim: f.claim, evidence: f.evidence, project: f.project ?? '',
  metric: f.metric ?? '', confidence: f.confidence,
})

const CONFIDENCE: Record<Confidence, [string, string]> = {
  high: ['High', 'b em'],
  medium: ['Medium', 'b am'],
  low: ['Low', 'b sl'],
}

const LEVELS: Confidence[] = ['high', 'medium', 'low']

const plural = (n: number, one: string, many: string) => (n === 1 ? one : many)

/** The count against the two thresholds: below min_hard nothing can score,
 * below min_warn verdicts are shakier than they look. */
function Coverage({ n, minHard, minWarn }: { n: number; minHard: number; minWarn: number }) {
  // The artboard's 24-wide bar is min_warn * 1.2; deriving it keeps the ticks
  // where they are drawn if a threshold ever moves.
  const scale = Math.max(minWarn * 1.2, n)
  const pct = (v: number) => `${Math.min(v / scale, 1) * 100}%`
  const short = minHard - n
  const more = minWarn - n

  const [tone, icon, message] =
    n < minHard
      ? ['ro', P.refused,
         <>Scoring and tailoring are blocked — add <span className="mono">{short}</span>{' '}
           more {plural(short, 'fact', 'facts')}</>]
      : n < minWarn
        ? ['am', P.refused,
           <>Scoring works — <span className="mono">{more}</span> more{' '}
             {plural(more, 'fact gives', 'facts give')} more reliable verdicts.</>]
        : ['em', P.allClear, <>Enough facts for reliable scoring</>]

  return (
    <section className={`card cov cov--${tone}`}>
      <div className="cov__count">
        <span className="mono cov__n">{n}</span>
        <span className="cov__unit">{plural(n, 'fact', 'facts')}</span>
      </div>
      <div className="cov__bar">
        <div className="cov__track">
          <div className="cov__fill" style={{ width: pct(n) }} />
          <span className="cov__tick" style={{ left: pct(minHard) }} />
          <span className="cov__tick" style={{ left: pct(minWarn) }} />
        </div>
        <div className="cov__scale">
          <span className="mono cov__label" style={{ left: pct(minHard) }}>{minHard} · scoring</span>
          <span className="mono cov__label" style={{ left: pct(minWarn) }}>{minWarn} · reliable</span>
        </div>
      </div>
      <p className="cov__msg">
        <Icon d={icon} size={16} />
        <span>{message}</span>
      </p>
    </section>
  )
}

function Filters({ facts, conf, setConf, cited, setCited, q, setQ }: {
  facts: Fact[]
  conf: Confidence | 'all'
  setConf: (c: Confidence | 'all') => void
  cited: boolean
  setCited: (b: boolean) => void
  q: string
  setQ: (s: string) => void
}) {
  // Counts come from the whole list, never the filtered view.
  const count = (c: Confidence | 'all') =>
    c === 'all' ? facts.length : facts.filter((f) => f.confidence === c).length

  return (
    <div className="filters">
      <div className="filters__chips">
        {(['all', ...LEVELS] as const).map((c) => (
          <button key={c} className="chip" aria-pressed={conf === c} onClick={() => setConf(c)}>
            {c === 'all' ? 'All' : CONFIDENCE[c][0]}
            <span className="mono">{count(c)}</span>
          </button>
        ))}
        <span className="filters__sep" />
        <button className="chip" aria-pressed={cited} onClick={() => setCited(!cited)}>
          Cited only
          <span className="tgl" aria-hidden="true"><span className="tgl__knob" /></span>
        </button>
      </div>
      <label className="filters__search">
        <Icon d={P.search} size={14} />
        <input className="field" type="search" value={q} placeholder="Filter claims and projects"
               aria-label="Filter claims and projects" onChange={(e) => setQ(e.target.value)} />
      </label>
    </div>
  )
}

/** Add (at the top of the list) and edit (in place, inside the fact's card)
 * are the same form. Keyed by fact id at the call site, so switching rows
 * remounts it with a fresh draft instead of syncing one in an effect. */
function FactForm({ fact, onSaved, onCancel }: {
  fact?: Fact
  onSaved: (message: string) => void
  onCancel: () => void
}) {
  const [draft, setDraft] = useState<Draft>(fact ? toDraft(fact) : EMPTY)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = (k: keyof Draft) => (v: string) => {
    setDraft((d) => ({ ...d, [k]: v }))
    setErrors(({ [k]: _drop, ...rest }) => rest)      // this field's error only
  }
  // Raw non-empty, not trimmed: a whitespace-only claim must still reach the
  // server, which is the only authority on what counts as filled.
  const filled = Boolean(draft.claim && draft.evidence)
  const dirty = !fact || JSON.stringify(draft) !== JSON.stringify(toDraft(fact))

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    setErrors({})
    const done = () => onSaved(fact ? 'Fact saved' : 'Fact added')
    const sent = fact ? put(`/api/facts/${fact.id}`, draft) : post('/api/facts', draft)
    sent
      .then(done)
      .catch((e: unknown) => {
        const body = e instanceof ApiError ? (e.body as { errors?: Record<string, string> }) : null
        setErrors(body?.errors ?? {})
        if (!body?.errors) setError(errorText(e))
      })
      .finally(() => setBusy(false))
  }

  const field = (k: keyof Draft, label: string, required = false, extra = '') => (
    <div className="form__f">
      <label className="fl" htmlFor={`f-${k}`}>
        {label}{required && <span className="req"> *</span>}
      </label>
      <input id={`f-${k}`} className={`field ${extra}`} type="text" value={draft[k]}
             disabled={busy} aria-invalid={Boolean(errors[k])}
             onChange={(e) => set(k)(e.target.value)} />
      {errors[k] && <span className="fe"><Icon d={P.refused} size={12} />{errors[k]}</span>}
    </div>
  )

  return (
    <form className="card form" onSubmit={save}>
      {field('claim', 'Claim', true)}
      <div className="form__f">
        <label className="fl" htmlFor="f-evidence">Evidence<span className="req"> *</span></label>
        <textarea id="f-evidence" className="field area" value={draft.evidence} disabled={busy}
                  aria-invalid={Boolean(errors.evidence)}
                  placeholder="what you shipped and the measurable result"
                  onChange={(e) => set('evidence')(e.target.value)} />
        {errors.evidence && (
          <span className="fe"><Icon d={P.refused} size={12} />{errors.evidence}</span>
        )}
      </div>
      <div className="form__grid">
        {field('project', 'Project')}
        {field('metric', 'Metric', false, 'mono')}
        <div className="form__f">
          <span className="fl">Confidence</span>
          <div className="seg seg--form" role="radiogroup" aria-label="Confidence">
            {LEVELS.map((c) => (
              <button key={c} type="button" role="radio" aria-checked={draft.confidence === c}
                      className={draft.confidence === c ? 'seg__opt is-on' : 'seg__opt'}
                      disabled={busy} onClick={() => set('confidence')(c)}>
                {CONFIDENCE[c][0]}
              </button>
            ))}
          </div>
        </div>
      </div>

      {fact && fact.cited_by.length > 0 && (
        <p className="form__note">
          {plural(fact.cited_by.length, 'Resume', 'Resumes')}{' '}
          <span className="mono">{fact.cited_by.join(', ')}</span>{' '}
          {plural(fact.cited_by.length, 'cites', 'cite')} this fact — your edit changes what
          they show.
        </p>
      )}
      {error && (
        <div className="alert" role="alert">
          <Icon d={P.refused} size={14} /><span>{error}</span>
        </div>
      )}
      <div className="form__actions">
        <button className="btn pri" type="submit" disabled={busy || !filled || !dirty}>
          {busy ? <Spinner /> : 'Save'}
        </button>
        <button className="btn" type="button" disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  )
}

function FactCard({ fact, onEdit, onDelete }: {
  fact: Fact
  onEdit: () => void
  onDelete: () => void
}) {
  const [label, tone] = CONFIDENCE[fact.confidence]
  const cited = fact.cited_by.length > 0
  // aria-disabled, not disabled: a disabled button shows no tooltip, and the
  // tooltip is where the reason lives.
  const why = cited ? `Cited by ${fact.cited_by.join(', ')} — edit it instead` : undefined

  return (
    <section className="card fact">
      <div className="fact__body">
        <p className="fact__claim">{fact.claim}</p>
        <p className="fact__ev">{fact.evidence}</p>
        {(fact.project || fact.metric || cited) && (
          <div className="fact__meta">
            {fact.project && <span className="meta">Project · {fact.project}</span>}
            {fact.metric && <span className="meta mono">{fact.metric}</span>}
            {cited && (
              <Link className="meta meta--cite" to="/resumes">
                Cited by <span className="mono">{fact.cited_by.join(', ')}</span>
              </Link>
            )}
          </div>
        )}
      </div>
      <div className="fact__side">
        <span className={tone}>{label}</span>
        <div className="fact__btns">
          <button className="btn sm" onClick={onEdit}>Edit</button>
          <button className="btn sm fact__del" aria-disabled={cited} title={why}
                  onClick={() => !cited && onDelete()}>Delete</button>
        </div>
        {cited && <span className="fact__cap">Cited — edit instead of deleting</span>}
        <div className="fact__menu">
          <Menu note={why} items={[
            { label: 'Edit', onSelect: onEdit },
            { label: 'Delete', danger: true, disabled: cited, onSelect: onDelete },
          ]} />
        </div>
      </div>
    </section>
  )
}

export function Facts() {
  const { toast } = useShell()
  const { data, error, reload } = usePoll<FactsData>('/api/facts', 0)
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [conf, setConf] = useState<Confidence | 'all'>('all')
  const [cited, setCited] = useState(false)
  const [q, setQ] = useState('')
  const [confirming, setConfirming] = useState<Fact | null>(null)
  const [deleting, setDeleting] = useState(false)

  const saved = (message: string) => {
    setEditing(null)
    toast({ text: message })
    reload()
  }

  const remove = (fact: Fact) => {
    setDeleting(true)
    del(`/api/facts/${fact.id}`)
      .then(() => toast({ text: 'Fact deleted' }))
      // A refusal (409 when a resume started citing it meanwhile) is shown as
      // the server wrote it, and the reload gives the card its Cited tag.
      .catch((e: unknown) => toast({ text: errorText(e), tone: 'error' }))
      .finally(() => {
        setDeleting(false)
        setConfirming(null)
        reload()
      })
  }

  if (!data) {
    return (
      <div className="page facts">
        <div className="page-head"><h1>Facts</h1></div>
        {error ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load facts:</b> {error}</span>
            <button className="btn sm" onClick={reload}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="facts__list">
            {[0, 1, 2].map((i) => <div key={i} className="skel fact-skel" />)}
          </div>
        )}
      </div>
    )
  }

  const { items, min_hard: minHard, min_warn: minWarn } = data
  const needle = q.trim().toLowerCase()
  const shown = items.filter((f) =>
    (conf === 'all' || f.confidence === conf)
    && (!cited || f.cited_by.length > 0)
    && (!needle || `${f.claim} ${f.project ?? ''}`.toLowerCase().includes(needle)))
  const filtered = shown.length !== items.length

  return (
    <div className="page facts">
      <TopBarActions>
        <button className="btn pri" onClick={() => setEditing('new')}>
          <Icon d={P.plus} size={14} />Add fact
        </button>
      </TopBarActions>

      <div className="page-head">
        <h1>Facts</h1>
        <p>Verified claims about your work. Only add what you can defend — resumes cite these.</p>
      </div>

      <Coverage n={items.length} minHard={minHard} minWarn={minWarn} />

      {items.length > 0 && (
        <Filters facts={items} conf={conf} setConf={setConf} cited={cited} setCited={setCited}
                 q={q} setQ={setQ} />
      )}

      <div className="facts__list">
        {editing === 'new' && (
          <FactForm key="new" onSaved={saved} onCancel={() => setEditing(null)} />
        )}

        {items.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.listChecks} size={20} /></span>
            <p>No facts yet — scoring needs at least {minHard}</p>
            <button className="btn pri" onClick={() => setEditing('new')}>Add fact</button>
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.search} size={20} /></span>
            <p>No facts match these filters</p>
            <button className="btn" onClick={() => { setConf('all'); setCited(false); setQ('') }}>
              Clear filters
            </button>
          </div>
        ) : (
          shown.map((f) => (
            editing === f.id
              ? <FactForm key={f.id} fact={f} onSaved={saved} onCancel={() => setEditing(null)} />
              : <FactCard key={f.id} fact={f} onEdit={() => setEditing(f.id)}
                          onDelete={() => setConfirming(f)} />
          ))
        )}
      </div>

      {filtered && shown.length > 0 && (
        <p className="facts__count">
          Showing <span className="mono">{shown.length}</span> of{' '}
          <span className="mono">{items.length}</span>
        </p>
      )}

      {confirming && (
        <ConfirmDialog
          title="Delete this fact?"
          text={items.length - 1 < minHard
            ? `You'll have ${items.length - 1} ${plural(items.length - 1, 'fact', 'facts')} — scoring and tailoring will be blocked.`
            : 'This removes the claim and its evidence.'}
          confirm="Delete"
          busy={deleting}
          onConfirm={() => remove(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </div>
  )
}
