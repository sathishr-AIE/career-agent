import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, del, errorText, get, put } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Switch, inCls } from '../components/FormPage'
import { Icon } from '../components/Icon'
import { Menu } from '../components/Menu'
import { useShell } from '../components/shell'
import { P } from '../icons'
import { localDay, parseUtc } from '../time'
import './Memory.css'

// -- shapes /api/memory returns (store.memory_list; MM1 adds the source job) --

interface MemoryItem {
  id: number
  /** A preference's memory_key, else the normalized question. */
  label: string
  answer: string
  kind: string | null
  options: string[] | null
  is_volatile: boolean
  last_confirmed_at: string | null
  use_count: number
  last_used_at: string | null
  /** A keyed row: the agent reads these before the literal questions. */
  is_preference: boolean
  source_job_id: number | null
  source_company: string | null
  source_title: string | null
}

interface MemoryResponse {
  items: MemoryItem[]
  /** How long a volatile answer stands before the agent re-asks it. */
  volatile_window_days: number
}

/** The acronyms the agent's own keys use, which the artboard shows in caps. */
const ACRONYMS = new Set(['ctc', 'inr', 'usd', 'us', 'uk', 'eu', 'hr', 'id', 'ats', 'pan', 'uan', 'gpa'])

/** `current_ctc_inr` becomes "Current CTC INR". A key's segments are tokens,
 * so an acronym in one is unambiguous -- unlike in a literal question, where
 * "us" is usually the word, so those only gain a capital. */
const humanize = (item: MemoryItem) => {
  if (!item.is_preference) return item.label.charAt(0).toUpperCase() + item.label.slice(1)
  const text = item.label.split('_').map((w) => (ACRONYMS.has(w) ? w.toUpperCase() : w)).join(' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Timestamps are SQLite's naive UTC, so both sides parse as UTC. */
const daysSince = (utc: string) => Math.floor((Date.now() - parseUtc(utc).getTime()) / 86_400_000)

const isStale = (item: MemoryItem, days: number) =>
  item.is_volatile && (!item.last_confirmed_at || daysSince(item.last_confirmed_at) > days)

/** Mono for a number-shaped answer, as the artboard sets the CTC rows. */
const numeric = (s: string) => /^\d[\d,.\s-]*$/.test(s)

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`

/** The label, its raw key and the kind badge — the same head in view and edit. */
function RowHead({ item }: { item: MemoryItem }) {
  return (
    <span className="mrow__head">
      <span className="mrow__label">{humanize(item)}</span>
      {item.is_preference && <span className="mono mrow__key">{item.label}</span>}
      <span className="b sl">{item.kind === 'choice' ? 'Choice' : 'Text'}</span>
    </span>
  )
}

/** One remembered answer, in place: the view, or its editor while editing. */
function MemoryRow({ item, days, editing, draft, setDraft, busy, error, onEdit, onCancel, onSave, onDelete }: {
  item: MemoryItem
  days: number
  editing: boolean
  draft: { answer: string; volatile: boolean }
  setDraft: (d: { answer: string; volatile: boolean }) => void
  busy: boolean
  error?: string
  onEdit: () => void
  onCancel: () => void
  onSave: () => void
  onDelete: () => void
}) {
  const reconfirm = `Re-confirm every ${plural(days, 'day')}`

  if (editing) {
    const id = `memory-${item.id}`
    const unchanged = draft.answer === item.answer && draft.volatile === item.is_volatile
    return (
      <div className="mrow mrow--edit">
        <div className="mrow__editor">
          <RowHead item={item} />
          <div className="mrow__controls">
            <span className="mrow__control">
              <label className="sr-only" htmlFor={id}>Answer for {humanize(item)}</label>
              {item.kind === 'choice' && item.options?.length ? (
                <select id={id} className={inCls(error)} value={draft.answer} disabled={busy}
                        onChange={(e) => setDraft({ ...draft, answer: e.target.value })}>
                  {/* An answer stored before the options changed still shows. */}
                  {!item.options.includes(draft.answer) && <option value={draft.answer}>{draft.answer}</option>}
                  {item.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input id={id} className={inCls(error, numeric(draft.answer) ? 'mono' : '')}
                       value={draft.answer} disabled={busy}
                       onChange={(e) => setDraft({ ...draft, answer: e.target.value })} />
              )}
            </span>
            <Switch label={reconfirm} checked={draft.volatile}
                    onChange={(v) => setDraft({ ...draft, volatile: v })} />
          </div>
          {error && <span className="err">{error}</span>}
          {item.is_preference && (
            <span className="help">Also updates this answer everywhere the agent learned it.</span>
          )}
          <span className="mrow__actions">
            <button type="button" className="btn pri" disabled={busy || !draft.answer.trim() || unchanged}
                    onClick={onSave}>Save</button>
            <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="mrow">
      <div className="mrow__text">
        <RowHead item={item} />
        <span className={numeric(item.answer) ? 'mono mrow__answer' : 'mrow__answer'}>{item.answer}</span>
        {item.is_volatile && (
          <span className="mrow__reconfirm">
            {isStale(item, days) ? (
              <>
                <span className="b am"><span className="dot dot--am" />Due to re-confirm</span>
                <span className="mrow__why">The agent will ask again, suggesting this answer.</span>
              </>
            ) : (
              <>
                <span className="b sl">{reconfirm}</span>
                <span className="mrow__why faint">
                  {item.last_confirmed_at
                    ? `confirmed ${plural(daysSince(item.last_confirmed_at), 'day')} ago`
                    : 'never confirmed'}
                </span>
              </>
            )}
          </span>
        )}
        {error && <span className="err">{error}</span>}
      </div>

      <div className="mrow__use">
        <span className="mono">
          Used {item.use_count}×{item.last_used_at && ` · last ${localDay(item.last_used_at)}`}
        </span>
        {item.source_company && (
          <span>
            Learned from{' '}
            {item.source_job_id ? (
              <Link to={`/jobs/${item.source_job_id}`}>{item.source_company} · {item.source_title}</Link>
            ) : (
              <>{item.source_company} · {item.source_title}</>
            )}
          </span>
        )}
      </div>

      <div className="mrow__side">
        <span className="mrow__actions">
          <button type="button" className="btn sm" onClick={onEdit}>Edit</button>
          <button type="button" className="btn sm ghost rose-text" onClick={onDelete}>Delete</button>
        </span>
        <span className="mrow__menu">
          <Menu items={[
            { label: 'Edit', icon: P.file, onSelect: onEdit },
            { label: 'Delete', icon: P.trash, danger: true, onSelect: onDelete },
          ]} />
        </span>
      </div>
    </div>
  )
}

/** Memory (approved Screen 11): the answers the agent reuses on application
 * forms instead of asking again. Your Profile always wins over these, and a
 * password or code never reaches this table (store._SECRET_QA_RE). */
export function Memory() {
  const { toast } = useShell()
  const [data, setData] = useState<MemoryResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [tab, setTab] = useState<'preferences' | 'known'>('preferences')
  const [filter, setFilter] = useState('')
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState({ answer: '', volatile: false })
  const [busy, setBusy] = useState(false)
  const [rowError, setRowError] = useState<Record<number, string>>({})
  const [doomed, setDoomed] = useState<MemoryItem | null>(null)

  const load = useCallback(
    () =>
      get<MemoryResponse>('/api/memory')
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

  if (!data) {
    return (
      <div className="page memory">
        <div className="page-head"><h1>Memory</h1></div>
        {loadError ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load your memory:</b> {loadError}</span>
            <button type="button" className="btn memory__retry" onClick={load}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="card memory__skel">
            {[0, 1, 2, 3].map((i) => (
              <div className="memory__skel-row" key={i}>
                <span className="skel" style={{ width: '40%' }} />
                <span className="skel" style={{ width: '22%' }} />
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  const { items, volatile_window_days: days } = data
  const prefs = items.filter((i) => i.is_preference)
  const known = items.filter((i) => !i.is_preference)
  const inTab = tab === 'preferences' ? prefs : known
  const q = filter.trim().toLowerCase()
  const shown = q ? inTab.filter((i) => `${humanize(i)} ${i.answer}`.toLowerCase().includes(q)) : inTab

  function edit(item: MemoryItem) {
    setEditing(item.id)
    setDraft({ answer: item.answer, volatile: item.is_volatile })
    setRowError({})
  }

  async function save(item: MemoryItem) {
    setBusy(true)
    setRowError({})
    try {
      await put(`/api/memory/${item.id}`, { answer: draft.answer, is_volatile: draft.volatile })
      setEditing(null)
      await load()
      toast({ text: 'Answer saved' })
    } catch (e: unknown) {
      // 422 (empty answer) and 404 (deleted meanwhile) both belong on the row.
      setRowError({ [item.id]: e instanceof ApiError ? e.message : errorText(e) })
    } finally {
      setBusy(false)
    }
  }

  async function forget(item: MemoryItem) {
    setBusy(true)
    try {
      await del(`/api/memory/${item.id}`)
      setDoomed(null)
      if (editing === item.id) setEditing(null)
      await load()
      toast({ text: 'Answer forgotten' })
    } catch (e: unknown) {
      setDoomed(null)
      setRowError({ [item.id]: e instanceof ApiError ? e.message : errorText(e) })
    } finally {
      setBusy(false)
    }
  }

  const tabs: [typeof tab, string, number][] = [
    ['preferences', 'Preferences', prefs.length],
    ['known', 'Known answers', known.length],
  ]

  return (
    <div className="page memory">
      <div className="page-head">
        <h1>Memory</h1>
        <p>
          Answers the agent reuses on application forms. Your <Link to="/profile">Profile</Link> always
          takes priority. Passwords and codes are never stored here.
        </p>
      </div>

      <div className="precedence">
        <span>When a form asks something, the agent uses the first of:</span>
        <span className="pill">Profile</span>
        <span className="precedence__arrow mono" aria-hidden="true">→</span>
        <span className="pill">Preferences</span>
        <span className="precedence__arrow mono" aria-hidden="true">→</span>
        <span className="pill">Known answers</span>
        <span className="precedence__arrow mono" aria-hidden="true">→</span>
        <span className="pill pill--ask">Asks you</span>
      </div>

      <section className="card">
        <div className="memory__head">
          <div className="tabs" role="tablist" aria-label="Memory">
            {tabs.map(([key, label, n]) => (
              <button key={key} type="button" role="tab" className="tab" aria-selected={tab === key}
                      onClick={() => {
                        setTab(key)
                        setEditing(null)
                      }}>
                {label}<span className="tab__count">{n}</span>
              </button>
            ))}
          </div>
          {items.length > 0 && (
            <span className="searchbox memory__filter">
              <Icon d={P.search} />
              <input className="field" value={filter} onChange={(e) => setFilter(e.target.value)}
                     placeholder="Filter questions and answers" aria-label="Filter memory" />
            </span>
          )}
        </div>

        {items.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.brain} size={16} /></span>
            Nothing remembered yet
            <span className="help">
              Tick “Remember this” when you answer the agent's questions in chat.
            </span>
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            No memories match
            <button type="button" className="btn" onClick={() => setFilter('')}>Clear filter</button>
          </div>
        ) : (
          shown.map((item) => (
            <MemoryRow key={item.id} item={item} days={days} editing={editing === item.id}
                       draft={draft} setDraft={setDraft} busy={busy} error={rowError[item.id]}
                       onEdit={() => edit(item)} onCancel={() => setEditing(null)}
                       onSave={() => save(item)} onDelete={() => setDoomed(item)} />
          ))
        )}
      </section>

      {doomed && (
        <ConfirmDialog
          title="Forget this answer?"
          text={`The agent will ask you next time a form needs it.${
            doomed.is_preference ? ' This also forgets it for every job it was learned from.' : ''}`}
          confirm="Forget" busy={busy}
          onConfirm={() => forget(doomed)} onCancel={() => setDoomed(null)} />
      )}
    </div>
  )
}
