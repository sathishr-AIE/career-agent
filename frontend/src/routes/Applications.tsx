import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, get, post, type ActionResult, type Job, type RunState } from '../api'
import { useAction } from '../components/useAction'
import { VerdictRail } from '../components/VerdictRail'
import '../components/ui.css'
import './Applications.css'

interface AppliedInfo {
  application_id: number
  outcome: string | null
}

interface ApplicationsContext {
  jobs: Job[]
  skipped_jobs: Job[]
  scheduled: boolean
  applied: Record<string, AppliedInfo>
  manual_types: string[]
  outcome_labels: Record<string, string>
  today: string
  submission_implemented: boolean
  tailored_sent: number
  outcomes_recorded: number
  run_state: RunState
  current_job: { job_id: number; company: string; title: string } | null
  stats: {
    total_applied: number
    queued: number
    in_progress: number
    successful: number
    failed_skipped: number
    resumable: number
  }
  recent_events: { type: string; payload: string | null; occurred_at: string }[]
  open_prompt: { id: number; kind: string; question: string; needs_answer: boolean } | null
  conversation_id: number | null
}

type Tab = 'queue' | 'all' | 'skipped'

function RunControls({ ctx, onChanged }: { ctx: ApplicationsContext; onChanged: () => void }) {
  const [mode, setMode] = useState<'auto' | 'manual'>(ctx.run_state.mode === 'auto' ? 'auto' : 'manual')
  const running = ctx.run_state.status === 'running'
  const s = ctx.run_state
  const { busy, error, run: act } = useAction(onChanged)

  return (
    <div className="card">
      <span className="mode-toggle">
        <label>
          <input type="radio" name="mode" checked={mode === 'manual'} disabled={running}
                onChange={() => setMode('manual')} />
          Manual
        </label>
        <label>
          <input type="radio" name="mode" checked={mode === 'auto'} disabled={running}
                onChange={() => setMode('auto')} />
          Auto
        </label>
      </span>
      <div>
        {(s.status === 'idle' || s.status === 'stopped' || s.status === 'error') && (
          <button className="btn primary" disabled={busy} onClick={() => act('/api/run/start', { mode })}>
            ▶ Start
          </button>
        )}
        {s.status === 'running' && (
          <>
            <button className="btn" disabled={busy} onClick={() => act('/api/run/pause')}>⏸ Pause</button>
            <button className="btn" disabled={busy} onClick={() => act('/api/run/stop')}>⏹ Stop</button>
          </>
        )}
        {s.status === 'paused' && (
          <>
            <button className="btn primary" disabled={busy} onClick={() => act('/api/run/resume')}>▶ Resume</button>
            <button className="btn" disabled={busy} onClick={() => act('/api/run/stop')}>⏹ Stop</button>
          </>
        )}{' '}
        <span className="rationale">{s.status}</span>
        {s.last_error && <div className="denied">{s.last_error}</div>}
        {error && <div className="denied" role="alert">{error}</div>}
      </div>

      <div className="stats-row">
        <div className="card"><h3>Total Applied</h3><div className="num">{ctx.stats.total_applied}</div></div>
        <div className="card"><h3>Queued</h3><div className="num">{ctx.stats.queued}</div></div>
        <div className="card"><h3>In Progress</h3><div className="num">{ctx.stats.in_progress}</div></div>
        <div className="card"><h3>Successful</h3><div className="num">{ctx.stats.successful}</div></div>
        <div className="card"><h3>Failed/Skipped</h3><div className="num">{ctx.stats.failed_skipped}</div></div>
        <div className="card"><h3>Interrupted</h3><div className="num">{ctx.stats.resumable}</div></div>
      </div>

      {ctx.current_job && (
        <div className="card now-processing">
          <div>
            <b>{ctx.current_job.title}</b> at {ctx.current_job.company}
          </div>
          {ctx.open_prompt && ctx.conversation_id ? (
            <span className="rationale">
              {ctx.open_prompt.question} <Link to={`/chat/${ctx.conversation_id}`}>Answer in chat →</Link>
            </span>
          ) : (
            <span className="rationale">Applying…</span>
          )}
        </div>
      )}

      <div className="activity-log">
        {ctx.recent_events.map((e, i) => (
          <div className="log-line" key={i}>
            {e.occurred_at} — {e.type}
            {e.payload ? `: ${e.payload}` : ''}
          </div>
        ))}
      </div>
    </div>
  )
}

function VerdictLabel({ verdict }: { verdict: Job['verdict'] }) {
  return <span className={`verdict-${verdict}`}>{verdict}</span>
}

const DIMENSIONS: { key: keyof Job; label: string }[] = [
  { key: 'role_fit', label: 'Role fit' },
  { key: 'credibility', label: 'Credibility' },
  { key: 'opportunity', label: 'Opportunity' },
  { key: 'application_quality', label: 'Application quality' },
  { key: 'eligibility_soft', label: 'Eligibility' },
]

function ScoreDetails({ job }: { job: Job }) {
  if (job.role_fit == null) return null
  return (
    <details>
      <summary className="rationale">Why this score</summary>
      <div className="kv">
        {DIMENSIONS.map((d) => (
          <div key={d.key}>
            <span>{d.label}</span>
            <b>{job[d.key] ?? '—'}</b>
          </div>
        ))}
      </div>
    </details>
  )
}

/** Shared by ActionCell and QueueRow: `run` fires-and-forgets an action,
 * `apply` additionally opens the job's chat, since the apply request lasts
 * the whole agent run (a CONFIRM waits on the human there). Both rows must
 * navigate on Apply, or the review card is invisible until you go find the
 * chat yourself. */
function useApply(onChanged: () => void) {
  const nav = useNavigate()
  const [msg, setMsg] = useState<ActionResult | null>(null)
  const run = (path: string, body?: unknown) =>
    post<ActionResult>(path, body)
      .then((r) => {
        setMsg(r)
        onChanged()
      })
      .catch((e) => setMsg({ ok: false, message: e instanceof ApiError ? e.message : 'Failed.' }))
  const openChat = (jobId: number) =>
    get<{ id: number }>(`/api/chat/jobs/${jobId}/conversation`).then((c) => nav(`/chat/${c.id}`))
  const apply = (jobId: number, path: string) => {
    run(path)
    openChat(jobId)
  }
  return { run, apply, openChat, msg }
}

function ActionCell({
  job, ctx, onChanged, variant,
}: {
  job: Job
  ctx: ApplicationsContext
  onChanged: () => void
  variant: 'all' | 'skipped'
}) {
  const { run, apply, openChat, msg } = useApply(onChanged)

  const tracked = ctx.applied[job.id]
  const untracked = !tracked && job.terminal_status !== 'held_unknown' &&
    job.terminal_status !== 'failed_permanent'

  return (
    <div>
      {job.resume_version?.startsWith('tailored-') && (
        <div>
          <a href={`/resume/${job.resume_version}`} target="_blank" rel="noreferrer">
            Download resume
          </a>
        </div>
      )}
      {msg && <div className={msg.ok ? 'done' : 'denied'}>{msg.message}</div>}
      {tracked ? (
        <OutcomeCell tracked={tracked} ctx={ctx} onChanged={onChanged} />
      ) : job.terminal_status === 'held_unknown' ? (
        <HeldCell job={job} today={ctx.today} onChanged={onChanged} run={run} />
      ) : job.terminal_status === 'failed_permanent' ? (
        <div className="denied">
          Failed permanently{job.failure_reason ? `: ${job.failure_reason}` : ''}
          <TranscriptLink applicationId={job.application_id} />
        </div>
      ) : (
        <MarkAppliedForm job={job} today={ctx.today} onChanged={onChanged} />
      )}
      {variant === 'skipped' ? (
        <button className="btn" onClick={() => run(`/api/override/${job.id}`)}
                title="Records an override of the gate's skip, and tracks it. Submission is manual.">
          Track anyway
        </button>
      ) : (
        untracked && (
          job.has_draft ? (
            <>
              <span className="rationale">Drafted</span>
              <button className="btn" onClick={() => openChat(job.id)}>Open draft chat</button>
              <button className="btn"
                      onClick={() => apply(job.id, job.verdict === 'skip' ? `/api/override/${job.id}` : `/api/apply/${job.id}`)}
                      title="Runs the apply agent again from the start, replacing the current draft.">
                Redo draft
              </button>
            </>
          ) : (
            <button className="btn"
                    onClick={() => apply(job.id, job.verdict === 'skip' ? `/api/override/${job.id}` : `/api/apply/${job.id}`)}>
              Apply
            </button>
          )
        )
      )}
      {variant === 'all' && <button onClick={() => run(`/api/dismiss/${job.id}`)}>Dismiss</button>}
    </div>
  )
}

function OutcomeCell({
  tracked, ctx, onChanged,
}: {
  tracked: AppliedInfo
  ctx: ApplicationsContext
  onChanged: () => void
}) {
  const [type, setType] = useState(ctx.manual_types[0] ?? '')
  const [occurredAt, setOccurredAt] = useState(ctx.today)
  const [notes, setNotes] = useState('')
  const { busy, error, run } = useAction(onChanged)
  return (
    <div>
      <span className={tracked.outcome ? 'done' : 'rationale'}>
        {ctx.outcome_labels[tracked.outcome ?? ''] ?? 'Awaiting response'}
      </span>
      <form
        className="outcome-form"
        onSubmit={(e) => {
          e.preventDefault()
          run(`/api/outcome/${tracked.application_id}`, { type, occurred_at: occurredAt, notes })
        }}
      >
        <select value={type} disabled={busy} onChange={(e) => setType(e.target.value)}>
          {ctx.manual_types.map((t) => (
            <option key={t} value={t}>
              {ctx.outcome_labels[t]}
            </option>
          ))}
        </select>
        <input type="date" value={occurredAt} disabled={busy} onChange={(e) => setOccurredAt(e.target.value)} />
        <input type="text" placeholder="notes (optional)" value={notes} disabled={busy}
              onChange={(e) => setNotes(e.target.value)} />
        <button className="btn" type="submit" disabled={busy}>Record</button>
      </form>
      {error && <div className="field-error" role="alert">{error}</div>}
    </div>
  )
}

// The agent drove a real browser and then stopped reporting, so this may or
// may not have been submitted. Both exits live here: neither worked before
// (queue_retry took only 'failed', Mark applied hit the live-application
// index), so raw SQL was the only way out of a state the label tells the
// user to clear.
/** application_id names the latest attempt; the route itself 404s if that
 * attempt never wrote a transcript, so this always offers the link rather
 * than guessing whether one exists. */
function TranscriptLink({ applicationId }: { applicationId: number | null }) {
  if (!applicationId) return null
  return (
    <div>
      <a href={`/api/transcript/${applicationId}`} target="_blank" rel="noreferrer">
        Transcript
      </a>
    </div>
  )
}

function HeldCell({
  job, today, onChanged, run,
}: {
  job: Job
  today: string
  onChanged: () => void
  run: (path: string) => void
}) {
  return (
    <div>
      <span className="denied">
        Held{job.failure_reason ? `: ${job.failure_reason}` : ''} — the agent may already have
        submitted this. Check the employer's site, then say which:
      </span>
      <TranscriptLink applicationId={job.application_id} />
      <MarkAppliedForm job={job} today={today} onChanged={onChanged} label="It was submitted" />
      <button
        className="btn"
        title="It never went through. Clears the hold and requeues the job."
        onClick={() => {
          if (window.confirm(
            'Confirm this application was NOT submitted. Clearing the hold lets the' +
            ' agent apply to this job again.')) {
            run(`/api/queue/${job.id}/retry?confirm=1`)
          }
        }}
      >
        Not submitted — clear hold
      </button>
    </div>
  )
}

function MarkAppliedForm({
  job, today, onChanged, label = 'Mark applied',
}: { job: Job; today: string; onChanged: () => void; label?: string }) {
  const [when, setWhen] = useState(today)
  const { busy, error, run } = useAction(onChanged)
  return (
    <div>
      <form
        className="outcome-form"
        onSubmit={(e) => {
          e.preventDefault()
          run(`/api/applied/${job.id}`, { when })
        }}
      >
        <input type="date" value={when} disabled={busy} onChange={(e) => setWhen(e.target.value)} />
        <button className="btn" type="submit" disabled={busy} title="You applied on the site yourself.">
          {label}
        </button>
      </form>
      {error && <div className="field-error" role="alert">{error}</div>}
    </div>
  )
}

function QueueRow({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const { run, apply, openChat, msg } = useApply(onChanged)
  return (
    <tr>
      <td style={{ width: 8 }}>
        <div style={{ height: '2.5rem' }}>
          <VerdictRail verdict={job.verdict} fraction={(job.score ?? 0) / 100} />
        </div>
      </td>
      <td className="data">{Math.round(job.score ?? 0)}</td>
      <td>
        <a href={job.url} target="_blank" rel="noreferrer">{job.title}</a> at {job.company}
        {job.has_draft && <span className="pill">Drafted</span>}
        {msg && <div className={msg.ok ? 'done' : 'denied'}>{msg.message}</div>}
      </td>
      <td>{job.source}</td>
      <td><VerdictLabel verdict={job.verdict} /></td>
      <td>
        {job.has_draft ? (
          <button className="btn" onClick={() => openChat(job.id)}>Open draft chat</button>
        ) : (
          <button className="btn" onClick={() => apply(job.id, `/api/apply/${job.id}`)} title="Draft this job now.">
            Apply
          </button>
        )}
        <button onClick={() => run(`/api/queue/${job.id}/priority`, { direction: 'up' })}>▲</button>
        <button onClick={() => run(`/api/queue/${job.id}/priority`, { direction: 'down' })}>▼</button>
        <button onClick={() => run(`/api/queue/${job.id}/skip`)}>Skip</button>
      </td>
    </tr>
  )
}

export function Applications() {
  const [ctx, setCtx] = useState<ApplicationsContext | null>(null)
  const [tab, setTab] = useState<Tab>('queue')

  // show=skipped is the one call that returns every verdict in `jobs`
  // (see career_agent/web/context.py's applications_context) -- all three
  // tabs render from this single fetch, same as the Jinja page always did.
  const reload = () => get<ApplicationsContext>('/api/applications?show=skipped').then(setCtx)

  useEffect(() => {
    reload()
    // Every action button tracks its own busy/error locally (useAction,
    // useApply), so a poll landing mid-click can't undo a disabled state or
    // lose in-progress form input -- it only refreshes read-only context.
    const id = setInterval(reload, 3000)
    return () => clearInterval(id)
  }, [])

  if (!ctx) return null

  const queueRows = ctx.jobs.filter(
    (j) => (j.verdict === 'submit' || j.verdict === 'hold') && !j.terminal_status,
  )

  return (
    <>
      <h1 className="page-title">Applications</h1>

      <RunControls ctx={ctx} onChanged={reload} />

      {ctx.submission_implemented && (
        <p className="rationale">
          {ctx.tailored_sent} tailored applications sent so far, {ctx.outcomes_recorded} outcomes
          recorded — the original plan for this feature said to wait for real evidence tailored
          applications convert before trusting this.
        </p>
      )}
      {!ctx.scheduled && (
        <p className="rationale">
          No scheduled run is installed, so nothing runs unless you start it. Run{' '}
          <code>career-agent run</code>, or install the daily task with{' '}
          <code>scripts\install-scheduler.ps1</code>.
        </p>
      )}

      <div className="tabs">
        <button className={tab === 'queue' ? 'active' : ''} onClick={() => setTab('queue')}>Queue</button>
        <button className={tab === 'all' ? 'active' : ''} onClick={() => setTab('all')}>All Applications</button>
        <button className={tab === 'skipped' ? 'active' : ''} onClick={() => setTab('skipped')}>Skipped</button>
      </div>

      {tab === 'queue' && (
        queueRows.length === 0 ? (
          <p className="rationale">Nothing to apply to — run discovery, or check Skipped.</p>
        ) : (
          <table>
            <thead>
              <tr><th></th><th>Score</th><th>Role</th><th>Source</th><th>Verdict</th><th></th></tr>
            </thead>
            <tbody>
              {queueRows.map((j) => (
                <QueueRow key={j.id} job={j} onChanged={reload} />
              ))}
            </tbody>
          </table>
        )
      )}

      {tab === 'all' && (
        <table>
          <thead>
            <tr><th>Score</th><th>Role</th><th>Source</th><th>Verdict</th><th></th></tr>
          </thead>
          <tbody>
            {ctx.jobs.map((j) => (
              <tr key={j.id}>
                <td className="data">{Math.round(j.score ?? 0)}</td>
                <td>
                  <a href={j.url} target="_blank" rel="noreferrer">{j.title}</a> at {j.company}
                  <br />
                  <span className="rationale">{j.rationale}</span>
                  <ScoreDetails job={j} />
                </td>
                <td>{j.source}</td>
                <td><VerdictLabel verdict={j.verdict} /></td>
                <td>
                  <ActionCell job={j} ctx={ctx} onChanged={reload} variant="all" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === 'skipped' && (
        <>
          <p className="rationale">
            These were skipped by the gate. Applying anyway is recorded as an override, which is
            how the gate gets caught being too strict.
          </p>
          <table>
            <thead>
              <tr><th>Score</th><th>Role</th><th>Source</th><th></th></tr>
            </thead>
            <tbody>
              {ctx.skipped_jobs.map((j) => (
                <tr key={j.id}>
                  <td className="data">{Math.round(j.score ?? 0)}</td>
                  <td>
                    <a href={j.url} target="_blank" rel="noreferrer">{j.title}</a> at {j.company}
                    <br />
                    <span className="rationale">{j.rationale}</span>
                    <ScoreDetails job={j} />
                  </td>
                  <td>{j.source}</td>
                  <td>
                    <ActionCell job={j} ctx={ctx} onChanged={reload} variant="skipped" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  )
}
