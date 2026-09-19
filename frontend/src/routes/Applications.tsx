import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { errorText, get, post, type Job, type RunStatusContext, type Verdict } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Icon, Spinner } from '../components/Icon'
import { Menu, type MenuItem } from '../components/Menu'
import { TopBarActions, useShell, type Toast } from '../components/shell'
import { useAction } from '../components/useAction'
import { usePoll } from '../components/usePoll'
import { describe, type Tone } from '../events'
import { P } from '../icons'
import { clock } from '../time'
import './Applications.css'

// -- shapes /api/applications?show=skipped returns (context.applications_context) --

interface AppliedInfo {
  application_id: number
  outcome: string | null
}

interface ApplicationsContext extends RunStatusContext {
  jobs: Job[]
  scheduled: boolean
  applied: Record<string, AppliedInfo>
  manual_types: string[]
  outcome_labels: Record<string, string>
  today: string
  tailored_sent: number
  outcomes_recorded: number
}

type Tab = 'queue' | 'all' | 'skipped'
type State = 'in_progress' | 'submitted' | 'held' | 'dismissed' | 'failed' | 'interrupted' | 'drafted' | 'queued'

/** One status per row, first match wins (the spec's row-state table). */
function stateOf(j: Job): State {
  if (j.terminal_status === 'in_flight') return 'in_progress'
  if (j.terminal_status === 'submitted') return 'submitted'
  if (j.terminal_status === 'held_unknown') return 'held'
  if (j.dismissed_at) return 'dismissed'
  if (j.terminal_status === 'failed_permanent') return 'failed'
  if (j.resumable) return 'interrupted'
  if (j.has_draft) return 'drafted'
  return 'queued'
}

const STATUS_CHIPS: [State | 'all', string][] = [
  ['all', 'All statuses'],
  ['queued', 'Queued'],
  ['in_progress', 'In progress'],
  ['drafted', 'Drafted'],
  ['submitted', 'Submitted'],
  ['held', 'Held'],
  ['failed', 'Failed'],
  ['interrupted', 'Interrupted'],
  ['dismissed', 'Dismissed'],
]
// Shown only when a row has that status: neither is in the approved chip row.
const OPTIONAL_CHIPS = new Set<string>(['interrupted', 'dismissed'])

const VERDICT: Record<Verdict, [string, Tone]> = { submit: ['Submit', 'em'], hold: ['Hold', 'am'], skip: ['Skip', 'ro'] }
const SOURCE: Record<string, string> = { linkedin: 'LinkedIn', naukri: 'Naukri', ats: 'Greenhouse' }
const OUTCOME_TONE: Record<string, Tone> = { screen: 'em', interview: 'em', offer: 'em', rejected: 'ro' }
const RUN_BADGE: Record<RunStatusContext['run_state']['status'], [string, Tone]> = {
  running: ['Running', 'sk'],
  paused: ['Paused', 'sl'],
  idle: ['Idle', 'sl'],
  stopped: ['Stopped', 'sl'],
  error: ['Error', 'ro'],
}
const DIMENSIONS: [keyof Job, string][] = [
  ['role_fit', 'Role fit'],
  ['credibility', 'Credibility'],
  ['opportunity', 'Opportunity'],
  ['application_quality', 'Application quality'],
  ['eligibility_soft', 'Eligibility'],
]

// ponytail: an Apply-style request lasts the whole agent run, so the page
// waits this long for a fast guard refusal before opening the job's chat. A
// refusal after that arrives as a toast (the shell's toasts survive navigation).
const REFUSAL_WAIT_MS = 1000

/** Queue order as the worker picks it: priority (NULL last), then score. */
const byQueueOrder = (a: Job, b: Job) =>
  (a.priority ?? Infinity) - (b.priority ?? Infinity) || (b.score ?? -1) - (a.score ?? -1)

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

// -- a row's actions --

interface RowEnv {
  ctx: ApplicationsContext
  reload: () => void
  toast: (t: Toast) => void
  expand: (id: number) => void
  openChat: (jobId: number) => void
}

/** A row's busy/refusal state plus its actions. Refusals show inline under the
 * row and as a toast (the spec's "each row action refusal"). */
function useRow(job: Job, env: RowEnv) {
  const { busy, error, run } = useAction(env.reload, (m) => env.toast({ text: m, tone: 'error' }))
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const act = (path: string, body?: unknown, done?: string) =>
    run(path, body).then((r) => {
      if (r !== undefined && done) env.toast({ text: done })
      return r
    })

  /** Apply, Redo draft, Track anyway: start the agent, then open its chat. */
  const startRun = async (path: string) => {
    setStarting(true)
    setStartError(null)
    const req = post(path)
    const early = await Promise.race([
      req.then(() => 'ok' as const, (e: unknown) => e),
      wait(REFUSAL_WAIT_MS).then(() => 'slow' as const),
    ])
    setStarting(false)
    if (early !== 'ok' && early !== 'slow') {
      const m = errorText(early)
      setStartError(m)
      env.toast({ text: m, tone: 'error' })
      return
    }
    req.catch((e: unknown) => env.toast({ text: errorText(e), tone: 'error' }))
    env.openChat(job.id)
  }

  const resume = () =>
    act(`/api/chat/jobs/${job.id}/resume`).then((r) => {
      if (r !== undefined) env.openChat(job.id)
    })

  return { busy: busy || starting, error: startError ?? error, act, startRun, resume }
}

type Row = ReturnType<typeof useRow>

const applyPath = (j: Job) => (j.verdict === 'skip' ? `/api/override/${j.id}` : `/api/apply/${j.id}`)

/** The one visible primary action for a row's state. */
function Primary({ job, state, row, env }: { job: Job; state: State; row: Row; env: RowEnv }) {
  const btn = (label: string, onClick: () => void) => (
    <button type="button" className="btn sm" disabled={row.busy} onClick={onClick}>
      {row.busy && <Spinner />}
      {label}
    </button>
  )
  switch (state) {
    case 'in_progress':
      return btn('Open chat', () => env.openChat(job.id))
    case 'submitted':
      return btn('Record outcome', () => env.expand(job.id))
    case 'held':
      return btn('It was submitted', () => env.expand(job.id))
    case 'dismissed':
      return btn('Undo', () => row.act(`/api/queue/${job.id}/restore`, undefined, 'Back in the queue'))
    case 'failed':
      return job.application_id ? (
        <a className="btn sm" href={`/api/transcript/${job.application_id}`} target="_blank" rel="noreferrer">
          Transcript
        </a>
      ) : null
    case 'interrupted':
      return btn('Continue', row.resume)
    case 'drafted':
      return btn('Open draft', () => env.openChat(job.id))
    case 'queued':
      return job.verdict === 'skip'
        ? btn('Track anyway', () => row.startRun(applyPath(job)))
        : btn('Apply', () => row.startRun(applyPath(job)))
  }
}

function StatusBadge({ job, state, ctx }: { job: Job; state: State; ctx: ApplicationsContext }) {
  const reason = job.failure_reason && <span className="reason mono">{job.failure_reason}</span>
  switch (state) {
    case 'in_progress':
      return ctx.open_prompt && ctx.current_job?.job_id === job.id ? (
        <span className="b am"><span className="dot dot--am" />Waiting on you</span>
      ) : (
        <span className="b sk"><span className="dot dot--sk" />In progress</span>
      )
    case 'submitted': {
      const outcome = ctx.applied[job.id]?.outcome
      return outcome ? (
        <span className={`b ${OUTCOME_TONE[outcome] ?? 'sl'}`}>{ctx.outcome_labels[outcome] ?? outcome}</span>
      ) : (
        <span className="b sl">Awaiting response</span>
      )
    }
    case 'held':
      return <span className="status-cell"><span className="b am">Held</span>{reason}</span>
    case 'failed':
      return <span className="status-cell"><span className="b ro">Failed</span>{reason}</span>
    case 'interrupted':
      return <span className="b am">Interrupted</span>
    case 'dismissed':
      return <span className="b sl">Dismissed</span>
    case 'drafted':
      return <span className="b sl">Drafted</span>
    case 'queued':
      return <span className="b sl">Queued</span>
  }
}

// -- expansion: why this score, the resume, the state's form --

function ScoreWhy({ job }: { job: Job }) {
  if (job.stage === 'hard') {
    return <p className="why-text">Hard filter: {job.rationale}</p>
  }
  const dims = DIMENSIONS.filter(([k]) => job[k] != null)
  return (
    <>
      {job.rationale && <p className="why-text">{job.rationale}</p>}
      {dims.length > 0 && (
        <div className="dims">
          {dims.map(([k, label]) => {
            const v = Math.round(job[k] as number)
            return (
              <div className="dim" key={k}>
                <span>{label}</span>
                <div className="dim__bar"><div style={{ width: `${v}%` }} /></div>
                <span className="mono">{v}</span>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}

function OutcomeForm({ job, row, ctx }: { job: Job; row: Row; ctx: ApplicationsContext }) {
  const tracked = ctx.applied[job.id]
  const [type, setType] = useState(ctx.manual_types[0] ?? '')
  const [date, setDate] = useState(ctx.today)
  const [notes, setNotes] = useState('')
  if (!tracked) return null
  return (
    <form className="row-form" onSubmit={(e) => {
      e.preventDefault()
      row.act(`/api/outcome/${tracked.application_id}`, { type, occurred_at: date, notes }, 'Outcome recorded')
    }}>
      <span className="lbl">Record outcome</span>
      <span className="row-form__current">
        Current: {tracked.outcome
          ? <b className={`tone-${OUTCOME_TONE[tracked.outcome] ?? 'sl'}`}>{ctx.outcome_labels[tracked.outcome] ?? tracked.outcome}</b>
          : 'Awaiting response'}
      </span>
      <div className="row-form__pair">
        <select className="field" value={type} disabled={row.busy} aria-label="Outcome"
                onChange={(e) => setType(e.target.value)}>
          {ctx.manual_types.map((t) => <option key={t} value={t}>{ctx.outcome_labels[t] ?? t}</option>)}
        </select>
        <input className="field mono" type="date" value={date} disabled={row.busy} aria-label="Date"
               onChange={(e) => setDate(e.target.value)} />
      </div>
      <input className="field" type="text" placeholder="Notes (optional)" value={notes} disabled={row.busy}
             aria-label="Notes" onChange={(e) => setNotes(e.target.value)} />
      <div><button type="submit" className="btn" disabled={row.busy}>Record</button></div>
    </form>
  )
}

function AppliedForm({ job, row, ctx, label }: { job: Job; row: Row; ctx: ApplicationsContext; label: string }) {
  const [date, setDate] = useState(ctx.today)
  return (
    <form className="row-form" onSubmit={(e) => {
      e.preventDefault()
      row.act(`/api/applied/${job.id}`, { when: date }, 'Marked applied')
    }}>
      <span className="lbl">{label}</span>
      <span className="row-form__current">
        {label === 'It was submitted'
          ? 'Check the employer’s site first. This records it as submitted on this date.'
          : 'You applied on the site yourself.'}
      </span>
      <div className="row-form__pair">
        <input className="field mono" type="date" value={date} disabled={row.busy} aria-label="Date"
               onChange={(e) => setDate(e.target.value)} />
        <button type="submit" className="btn" disabled={row.busy}>{label}</button>
      </div>
    </form>
  )
}

function Expansion({ job, state, row, ctx, tab }: { job: Job; state: State; row: Row; ctx: ApplicationsContext; tab: Tab }) {
  const tailored = job.resume_version?.startsWith('tailored-')
  let form: ReactNode = null
  if (state === 'submitted') form = <OutcomeForm job={job} row={row} ctx={ctx} />
  else if (state === 'held') form = <AppliedForm job={job} row={row} ctx={ctx} label="It was submitted" />
  else if (tab === 'all' && (state === 'queued' || state === 'drafted')) {
    form = <AppliedForm job={job} row={row} ctx={ctx} label="Mark applied" />
  }
  return (
    <tr className="expansion">
      <td colSpan={8}>
        <div className="expansion__grid">
          <div className="expansion__col">
            <span className="lbl">Why this score</span>
            <ScoreWhy job={job} />
          </div>
          <div className="expansion__col">
            <span className="lbl">{state === 'submitted' ? 'Resume sent' : 'Resume'}</span>
            {job.resume_version ? (
              <span className="mono resume-version">{job.resume_version}</span>
            ) : (
              <span className="muted-text">Not tailored yet — tailoring runs on first Apply</span>
            )}
            {tailored && (
              <a className="icon-link" href={`/resume/${job.resume_version}`}>
                <Icon d={P.download} />Download .docx
              </a>
            )}
            {/* Screen 4 turns this into "Open job →" (the job hub). */}
            <a className="icon-link" href={job.url} target="_blank" rel="noreferrer">Open posting ↗</a>
          </div>
          <div className="expansion__col">{form}</div>
        </div>
      </td>
    </tr>
  )
}

// -- one row in the Queue or All table --

function AppRow({ job, env, tab, expanded, onToggle, edge }: {
  job: Job
  env: RowEnv
  tab: Tab
  expanded: boolean
  onToggle: () => void
  /** Queue tab only: whether this row is first/last, to disable Move up/down. */
  edge?: { first: boolean; last: boolean }
}) {
  const row = useRow(job, env)
  const state = stateOf(job)
  const [confirming, setConfirming] = useState(false)
  const v = job.verdict ? VERDICT[job.verdict] : null

  let items: (MenuItem | 'sep')[] = []
  let note: string | undefined
  const move = (direction: 'up' | 'down') => row.act(`/api/queue/${job.id}/priority`, { direction })
  const markApplied: MenuItem = { label: 'Mark applied…', icon: P.check, onSelect: () => env.expand(job.id) }
  const dismiss: MenuItem = {
    label: 'Dismiss', icon: P.x, danger: true,
    onSelect: () => row.act(`/api/dismiss/${job.id}`, undefined, 'Dismissed'),
  }
  if (state === 'queued') {
    items = [
      { label: 'Move up', icon: P.up, disabled: edge?.first, onSelect: () => move('up') },
      { label: 'Move down', icon: P.down, disabled: edge?.last, onSelect: () => move('down') },
      ...(tab === 'all' ? [markApplied] : []),
      'sep',
      { label: 'Skip this job', icon: P.arrowRight, danger: true,
        onSelect: () => row.act(`/api/queue/${job.id}/skip`, undefined, 'Skipped — moved out of the queue') },
    ]
  } else if (state === 'drafted') {
    items = [
      { label: 'Redo draft', icon: P.undo, onSelect: () => row.startRun(applyPath(job)) },
      ...(tab === 'all' ? [markApplied, 'sep' as const, dismiss] : []),
    ]
  } else if (state === 'held') {
    note = 'The agent may already have submitted this. Check the employer’s site first.'
    items = [
      ...(job.application_id ? [{
        label: 'View transcript', icon: P.transcript,
        onSelect: () => window.open(`/api/transcript/${job.application_id}`, '_blank', 'noreferrer'),
      }] : []),
      'sep',
      { label: 'Not submitted — clear hold…', icon: P.undo, danger: true, onSelect: () => setConfirming(true) },
    ]
  } else if (state === 'failed') {
    items = [dismiss]
  }

  return (
    <>
      <tr className={expanded ? 'app-row is-open' : 'app-row'}>
        <td className="td c-chev">
          <button type="button" className="chev" aria-expanded={expanded}
                  aria-label={expanded ? 'Hide details' : 'Show details'} onClick={onToggle}>
            <Icon d={expanded ? P.chevronDown : P.chevronRight} />
          </button>
        </td>
        <td className="td c-company">
          <span className="company"><span className="logo">{job.company.charAt(0).toUpperCase()}</span>
            <span className="clip" title={job.company}>{job.company}</span></span>
        </td>
        <td className="td c-role"><span className="clip role" title={job.title}>{job.title}</span></td>
        <td className="td c-source"><span className="src">{SOURCE[job.source] ?? job.source}</span></td>
        <td className="td c-verdict">{v && <span className={`b ${v[1]}`}>{v[0]}</span>}</td>
        <td className="td c-score mono">
          {job.score != null ? <>{Math.round(job.score)}<span className="score-of">/100</span></> : <span className="faint">—</span>}
        </td>
        <td className="td c-status"><StatusBadge job={job} state={state} ctx={env.ctx} /></td>
        <td className="td c-actions">
          <span className="actions">
            <Primary job={job} state={state} row={row} env={env} />
            {items.length > 0 ? <Menu items={items} note={note} /> : <span className="ib-spacer" />}
          </span>
        </td>
      </tr>
      {expanded && <Expansion job={job} state={state} row={row} ctx={env.ctx} tab={tab} />}
      {row.error && (
        <tr className="refusal-row">
          <td colSpan={8}>
            <div className="alert alert--row" role="alert">
              <Icon d={P.refused} />
              <span><b>Refused:</b> {row.error}</span>
            </div>
          </td>
        </tr>
      )}
      {confirming && (
        <ConfirmDialog
          title="Clear the hold?"
          text="Confirm this application was NOT submitted. Clearing the hold lets the agent apply to this job again."
          confirm="Not submitted — clear hold"
          busy={row.busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false)
            row.act(`/api/queue/${job.id}/retry?confirm=1`, undefined, 'Hold cleared — requeued')
          }}
        />
      )}
    </>
  )
}

function AppTable({ rows, env, tab, open, toggle, empty }: {
  rows: Job[]
  env: RowEnv
  tab: Tab
  open: Set<number>
  toggle: (id: number) => void
  empty: ReactNode
}) {
  if (rows.length === 0) return <div className="empty">{empty}</div>
  return (
    <table className="apps-table">
      <thead>
        <tr>
          <th className="th c-chev" aria-label="Details" />
          <th className="th">Company</th>
          <th className="th">Role</th>
          <th className="th c-source">Source</th>
          <th className="th">Verdict</th>
          <th className="th">Score</th>
          <th className="th">Status</th>
          <th className="th c-actions">Actions</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((j, i) => (
          <AppRow key={j.id} job={j} env={env} tab={tab} expanded={open.has(j.id)} onToggle={() => toggle(j.id)}
                  edge={tab === 'queue' ? { first: i === 0, last: i === rows.length - 1 } : undefined} />
        ))}
      </tbody>
    </table>
  )
}

// -- Skipped tab --

function GateRow({ job, env }: { job: Job; env: RowEnv }) {
  const row = useRow(job, env)
  const state = stateOf(job)
  return (
    <>
      <tr className="app-row">
        <td className="td c-company">
          <span className="company"><span className="logo">{job.company.charAt(0).toUpperCase()}</span>
            <span className="clip" title={job.company}>{job.company}</span></span>
        </td>
        <td className="td c-role"><span className="clip role" title={job.title}>{job.title}</span></td>
        <td className="td c-source"><span className="src">{SOURCE[job.source] ?? job.source}</span></td>
        <td className="td c-score mono">
          {job.score != null ? <>{Math.round(job.score)}<span className="score-of">/100</span></> : <span className="faint">—</span>}
        </td>
        <td className="td c-why"><span className="why" title={job.rationale ?? ''}>{job.rationale}</span></td>
        <td className="td c-actions">
          <span className="actions"><Primary job={job} state={state} row={row} env={env} /></span>
        </td>
      </tr>
      {row.error && (
        <tr className="refusal-row">
          <td colSpan={6}>
            <div className="alert alert--row" role="alert"><Icon d={P.refused} /><span><b>Refused:</b> {row.error}</span></div>
          </td>
        </tr>
      )}
    </>
  )
}

function Skipped({ jobs, env }: { jobs: Job[]; env: RowEnv }) {
  const gate = jobs.filter((j) => j.stage !== 'hard')
  const hard = jobs.filter((j) => j.stage === 'hard')
  if (jobs.length === 0) {
    return (
      <div className="empty">
        <span className="empty__icon"><Icon d={P.inbox} size={16} /></span>
        No skipped jobs yet
      </div>
    )
  }
  return (
    <>
      <p className="skipped-intro">
        Applying to a gate skip anyway is recorded as an override — that’s how a gate that’s too strict gets caught.
        Hard-filter skips are final.
      </p>
      <div className="section-head">
        <span className="ct">Gate skipped</span>
        <span className="mono faint">{gate.length}</span>
        <span className="section-head__sub">· scored below the threshold</span>
      </div>
      {gate.length > 0 && (
        <table className="apps-table skip-table">
          <thead>
            <tr>
              <th className="th">Company</th><th className="th">Role</th><th className="th c-source">Source</th>
              <th className="th">Score</th><th className="th">Why</th><th className="th c-actions">Actions</th>
            </tr>
          </thead>
          <tbody>{gate.map((j) => <GateRow key={j.id} job={j} env={env} />)}</tbody>
        </table>
      )}
      <div className="section-head section-head--rule">
        <span className="ct">Hard filter</span>
        <span className="mono faint">{hard.length}</span>
        <span className="section-head__sub">· never scored, no model call</span>
      </div>
      {hard.length > 0 && (
        <table className="apps-table skip-table">
          <thead>
            <tr>
              <th className="th">Company</th><th className="th">Role</th><th className="th c-source">Source</th>
              <th className="th">Score</th><th className="th">Reason</th>
            </tr>
          </thead>
          <tbody>
            {hard.map((j) => (
              <tr className="app-row" key={j.id}>
                <td className="td c-company">
                  <span className="company"><span className="logo">{j.company.charAt(0).toUpperCase()}</span>
                    <span className="clip" title={j.company}>{j.company}</span></span>
                </td>
                <td className="td c-role"><span className="clip role" title={j.title}>{j.title}</span></td>
                <td className="td c-source"><span className="src">{SOURCE[j.source] ?? j.source}</span></td>
                <td className="td c-score mono faint">—</td>
                <td className="td c-why"><span className="why reason-text" title={j.rationale ?? ''}>{j.rationale}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// -- run controls (top bar) and the run strip --

function RunControls({ ctx, busy, act }: {
  ctx: ApplicationsContext
  busy: boolean
  act: (path: string, body?: unknown) => void
}) {
  const s = ctx.run_state.status
  const [mode, setMode] = useState<'manual' | 'auto'>(ctx.run_state.mode === 'auto' ? 'auto' : 'manual')
  const locked = s === 'running'
  const shown = locked ? (ctx.run_state.mode ?? mode) : mode
  return (
    <>
      <div className="seg" role="radiogroup" aria-label="Apply mode"
           title={locked ? 'Mode is locked while the run is running' : undefined}>
        {(['manual', 'auto'] as const).map((m) => (
          <button key={m} type="button" role="radio" aria-checked={shown === m} disabled={locked}
                  className={shown === m ? 'seg__opt is-on' : 'seg__opt'} onClick={() => setMode(m)}>
            {m === 'manual' ? 'Manual' : 'Auto'}
          </button>
        ))}
        {locked && <span className="seg__lock"><Icon d={P.lock} size={12} /></span>}
      </div>
      {(s === 'idle' || s === 'stopped' || s === 'error') && (
        <button type="button" className="btn pri" disabled={busy} onClick={() => act('/api/run/start', { mode })}>
          {busy ? <Spinner /> : <Icon d={P.play} size={14} fill />}Start
        </button>
      )}
      {s === 'running' && (
        <button type="button" className="btn" disabled={busy} onClick={() => act('/api/run/pause')}>
          <span className="icon-muted"><Icon d={P.pause} size={14} fill /></span>Pause
        </button>
      )}
      {s === 'paused' && (
        <button type="button" className="btn pri" disabled={busy} onClick={() => act('/api/run/resume')}>
          <Icon d={P.play} size={14} fill />Resume
        </button>
      )}
      {(s === 'running' || s === 'paused') && (
        <button type="button" className="btn rose-text" disabled={busy} onClick={() => act('/api/run/stop')}>
          <Icon d={P.stop} size={14} fill />Stop
        </button>
      )}
    </>
  )
}

function RunStrip({ ctx, refusal }: { ctx: ApplicationsContext; refusal: string | null }) {
  const [eventsOpen, setEventsOpen] = useState(false)
  const rs = ctx.run_state
  const [label, tone] = RUN_BADGE[rs.status]
  const active = rs.status === 'running' || rs.status === 'paused'
  const stats: [string, number][] = [
    ['Total applied', ctx.stats.total_applied],
    ['Queued', ctx.stats.queued],
    ['In progress', ctx.stats.in_progress],
    ['Successful', ctx.stats.successful],
    ['Failed / skipped', ctx.stats.failed_skipped],
    ['Interrupted', ctx.stats.resumable],
  ]
  return (
    <section className="card run-strip">
      <div className="run-strip__head">
        <div className="run-strip__left">
          <span className={`b ${tone}`}>
            {(rs.status === 'running' || rs.status === 'error') && <span className={`dot dot--${tone}`} />}
            {label}
          </span>
          {rs.mode && active && <span className="mode-chip mono">{rs.mode}</span>}
          {ctx.current_job && active && (
            <span className="run-strip__now">
              Now: <b>{ctx.current_job.company} · {ctx.current_job.title}</b>
            </span>
          )}
          {ctx.open_prompt && ctx.conversation_id && (
            // Interim target: the job's chat. Screen 6 moves it to the job hub's Chat tab.
            <span className="b am">
              <span className="dot dot--am" />Card waiting · <Link to={`/chat/${ctx.conversation_id}`}>Answer →</Link>
            </span>
          )}
        </div>
        <button type="button" className="btn sm ghost" aria-expanded={eventsOpen}
                onClick={() => setEventsOpen((o) => !o)}>
          Recent events<Icon d={eventsOpen ? P.chevronDown : P.chevronRight} />
        </button>
      </div>
      <div className="run-stats">
        {stats.map(([l, n]) => (
          <div className="run-stat" key={l}>
            <span className="lbl">{l}</span>
            <span className="mono run-stat__num">{n}</span>
          </div>
        ))}
      </div>
      {(rs.last_error || refusal) && (
        <div className="run-strip__alerts">
          {rs.last_error && (
            <div className="alert" role="alert"><Icon d={P.refused} size={16} /><span><b>Run error:</b> {rs.last_error}</span></div>
          )}
          {refusal && (
            <div className="alert" role="alert"><Icon d={P.refused} size={16} /><span><b>Refused:</b> {refusal}</span></div>
          )}
        </div>
      )}
      {eventsOpen && (
        <div className="run-events">
          {ctx.recent_events.length === 0 ? (
            <span className="muted-text">No apply events yet.</span>
          ) : (
            ctx.recent_events.map((e, i) => (
              <div className="run-event" key={i}>
                <span className="run-event__label">{describe(e)[0]}</span>
                {e.payload && <span className="mono run-event__payload" title={e.payload}>{e.payload}</span>}
                <span className="mono run-event__time">{clock(e.occurred_at)}</span>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  )
}

function Skeleton() {
  return (
    <div className="card" aria-busy="true">
      <div className="tabs"><span className="tab"><span className="skel" style={{ width: 160 }} /></span></div>
      {Array.from({ length: 6 }, (_, i) => (
        <div className="skel-row" key={i}>
          <span className="skel" style={{ width: 22 }} />
          <span className="skel" style={{ width: '18%' }} />
          <span className="skel" style={{ width: '28%' }} />
          <span className="skel" style={{ width: 48 }} />
          <span className="skel" style={{ width: 60 }} />
        </div>
      ))}
    </div>
  )
}

/** Applications (approved Screen 3): work the queue, follow every application
 * to an outcome, override gate skips. One /api/applications?show=skipped poll
 * (every verdict) feeds all three tabs, filtered on the client. */
export function Applications() {
  const { data: ctx, error, reload } = usePoll<ApplicationsContext>('/api/applications?show=skipped')
  const { toast } = useShell()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const [open, setOpen] = useState<Set<number>>(() => new Set())
  const runCtl = useAction(reload, (m) => toast({ text: m, tone: 'error' }))

  const tabParam = params.get('tab')
  const tab: Tab = tabParam === 'all' || tabParam === 'skipped' ? tabParam : 'queue'
  const status = params.get('status') ?? 'all'

  const toggle = (id: number) =>
    setOpen((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const expand = (id: number) => setOpen((s) => (s.has(id) ? s : new Set(s).add(id)))
  const openChat = (jobId: number) =>
    get<{ id: number }>(`/api/chat/jobs/${jobId}/conversation`)
      .then((c) => nav(`/chat/${c.id}`))
      .catch((e: unknown) => toast({ text: errorText(e), tone: 'error' }))

  const topBar = ctx && (
    <TopBarActions>
      <RunControls ctx={ctx} busy={runCtl.busy} act={(path, body) => void runCtl.run(path, body)} />
    </TopBarActions>
  )

  if (!ctx) {
    return (
      <div className="page apps">
        <div className="page-head"><h1>Applications</h1><p>Roles scored against your career brief</p></div>
        {error ? (
          <div className="alert" role="alert"><Icon d={P.refused} size={16} /><span><b>Can't load applications:</b> {error}</span></div>
        ) : (
          <Skeleton />
        )}
      </div>
    )
  }

  const env: RowEnv = { ctx, reload, toast, expand, openChat }
  const tracked = ctx.jobs.filter((j) => j.verdict === 'submit' || j.verdict === 'hold')
  const skipped = ctx.jobs.filter((j) => j.verdict === 'skip')
  const queue = tracked.filter((j) => {
    const s = stateOf(j)
    return s === 'queued' || s === 'drafted'
  }).sort(byQueueOrder)
  const counts: Record<string, number> = {}
  for (const j of tracked) {
    const s = stateOf(j)
    counts[s] = (counts[s] ?? 0) + 1
  }
  const visible = tracked.filter((j) => stateOf(j) !== 'dismissed')
  counts.all = visible.length
  const allRows = status === 'all' ? visible : tracked.filter((j) => stateOf(j) === status)

  const tabs: [Tab, string, number][] = [
    ['all', 'All', visible.length],
    ['queue', 'Queue', queue.length],
    ['skipped', 'Skipped', skipped.length],
  ]

  return (
    <div className="page apps">
      {topBar}
      <div className="page-head"><h1>Applications</h1><p>Roles scored against your career brief</p></div>

      {!ctx.scheduled && (
        <div className="notice">
          <Icon d={P.info} size={16} />
          <span>
            No scheduled run is installed — nothing runs unless you start it. Run <code>career-agent run</code> or
            install the daily task with <code>scripts\install-scheduler.ps1</code>.
          </span>
        </div>
      )}
      {ctx.submission_implemented && (
        <p className="tailored-line">
          {ctx.tailored_sent} tailored applications sent so far, {ctx.outcomes_recorded} outcomes recorded.
        </p>
      )}

      <RunStrip ctx={ctx} refusal={runCtl.error} />

      <section className="card apps-card">
        <div className="tabs" role="tablist" aria-label="Applications">
          {tabs.map(([key, label, n]) => (
            <button key={key} type="button" role="tab" className="tab" aria-selected={tab === key}
                    onClick={() => setParams({ tab: key })}>
              {label}<span className="tab__count">{n}</span>
            </button>
          ))}
        </div>

        {tab === 'queue' && (
          <AppTable rows={queue} env={env} tab="queue" open={open} toggle={toggle}
                    empty={<>
                      <span className="empty__icon"><Icon d={P.inbox} size={16} /></span>
                      <span>Nothing to apply to — run the pipeline, or check{' '}
                        <button type="button" className="link-btn" onClick={() => setParams({ tab: 'skipped' })}>Skipped</button>
                      </span>
                    </>} />
        )}

        {tab === 'all' && (
          <>
            <div className="chips">
              {STATUS_CHIPS.filter(([k]) => !OPTIONAL_CHIPS.has(k) || counts[k]).map(([k, label]) => (
                <button key={k} type="button" className="chip" aria-pressed={status === k}
                        onClick={() => setParams({ tab: 'all', status: k }, { replace: true })}>
                  {label}<span className="mono">{counts[k] ?? 0}</span>
                </button>
              ))}
            </div>
            <AppTable rows={allRows} env={env} tab="all" open={open} toggle={toggle}
                      empty={status === 'all' ? 'No applications yet' : 'No applications with this status'} />
          </>
        )}

        {tab === 'skipped' && <Skipped jobs={skipped} env={env} />}
      </section>
    </div>
  )
}
