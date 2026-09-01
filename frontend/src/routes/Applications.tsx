import { useEffect, useState } from 'react'
import { ApiError, get, post, type ActionResult, type Job, type RunState } from '../api'
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
  }
  recent_events: { type: string; payload: string | null; occurred_at: string }[]
  needs_answer_question: string | null
  draft_answers: Record<string, unknown> | null
}

type Tab = 'queue' | 'all' | 'skipped'

function RunControls({ ctx, onChanged }: { ctx: ApplicationsContext; onChanged: () => void }) {
  const [mode, setMode] = useState<'auto' | 'manual'>(ctx.run_state.mode === 'auto' ? 'auto' : 'manual')
  const running = ctx.run_state.status === 'running'
  const s = ctx.run_state

  const act = (path: string, body?: unknown) => post(path, body).then(onChanged)

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
          <button className="btn primary" onClick={() => act('/api/run/start', { mode })}>
            ▶ Start
          </button>
        )}
        {s.status === 'running' && (
          <>
            <button className="btn" onClick={() => act('/api/run/pause')}>⏸ Pause</button>
            <button className="btn" onClick={() => act('/api/run/stop')}>⏹ Stop</button>
          </>
        )}
        {s.status === 'paused' && (
          <>
            <button className="btn primary" onClick={() => act('/api/run/resume')}>▶ Resume</button>
            <button className="btn" onClick={() => act('/api/run/stop')}>⏹ Stop</button>
          </>
        )}{' '}
        <span className="rationale">{s.status}</span>
        {s.last_error && <div className="denied">{s.last_error}</div>}
      </div>

      <div className="stats-row">
        <div className="card"><h3>Total Applied</h3><div className="num">{ctx.stats.total_applied}</div></div>
        <div className="card"><h3>Queued</h3><div className="num">{ctx.stats.queued}</div></div>
        <div className="card"><h3>In Progress</h3><div className="num">{ctx.stats.in_progress}</div></div>
        <div className="card"><h3>Successful</h3><div className="num">{ctx.stats.successful}</div></div>
        <div className="card"><h3>Failed/Skipped</h3><div className="num">{ctx.stats.failed_skipped}</div></div>
      </div>

      {ctx.current_job && (
        <div className="card now-processing">
          <div>
            <b>{ctx.current_job.title}</b> at {ctx.current_job.company}
          </div>
          {ctx.needs_answer_question ? (
            <AnswerForm jobId={ctx.current_job.job_id} question={ctx.needs_answer_question} onSaved={onChanged} />
          ) : mode === 'manual' && s.mode === 'manual' ? (
            <form onSubmit={(e) => e.preventDefault()}>
              <span className="rationale">Draft ready — review and send.</span>
              <button className="btn" onClick={() => act(`/api/send/${ctx.current_job!.job_id}`)}>Send</button>
              <button className="btn" onClick={() => act(`/api/queue/${ctx.current_job!.job_id}/skip`)}>Skip</button>
            </form>
          ) : (
            <span className="rationale">Applying…</span>
          )}
          {ctx.draft_answers && (
            <pre className="draft-answers">{JSON.stringify(ctx.draft_answers, null, 2)}</pre>
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

function AnswerForm({ jobId, question, onSaved }: { jobId: number; question: string; onSaved: () => void }) {
  const [answer, setAnswer] = useState('')
  const [isVolatile, setIsVolatile] = useState(false)
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        post(`/api/answer/${jobId}`, { question, answer, is_volatile: isVolatile }).then(onSaved)
      }}
    >
      <span className="rationale">Answer needed: {question}</span>
      <input type="text" placeholder="Your answer" required value={answer}
            onChange={(e) => setAnswer(e.target.value)} />
      <label>
        <input type="checkbox" checked={isVolatile} onChange={(e) => setIsVolatile(e.target.checked)} />
        This may change later
      </label>
      <button className="btn" type="submit">Save answer</button>
    </form>
  )
}

function VerdictLabel({ verdict }: { verdict: Job['verdict'] }) {
  return <span className={`verdict-${verdict}`}>{verdict}</span>
}

function ActionCell({
  job, ctx, onChanged, variant,
}: {
  job: Job
  ctx: ApplicationsContext
  onChanged: () => void
  variant: 'all' | 'skipped'
}) {
  const [msg, setMsg] = useState<ActionResult | null>(null)
  const run = (path: string) =>
    post<ActionResult>(path)
      .then((r) => {
        setMsg(r)
        onChanged()
      })
      .catch((e) => setMsg({ ok: false, message: e instanceof ApiError ? e.message : 'Failed.' }))

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
        <span className="denied">Held — confirm manually, then clear it</span>
      ) : job.terminal_status === 'failed_permanent' ? (
        <span className="denied">Failed permanently — see the event log</span>
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
            <span className="rationale">Drafted — review in the status card above.</span>
          ) : (
            <button className="btn"
                    onClick={() => run(job.verdict === 'skip' ? `/api/override/${job.id}` : `/api/apply/${job.id}`)}>
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
  return (
    <div>
      <span className={tracked.outcome ? 'done' : 'rationale'}>
        {ctx.outcome_labels[tracked.outcome ?? ''] ?? 'Awaiting response'}
      </span>
      <form
        className="outcome-form"
        onSubmit={(e) => {
          e.preventDefault()
          post(`/api/outcome/${tracked.application_id}`, { type, occurred_at: occurredAt, notes }).then(onChanged)
        }}
      >
        <select value={type} onChange={(e) => setType(e.target.value)}>
          {ctx.manual_types.map((t) => (
            <option key={t} value={t}>
              {ctx.outcome_labels[t]}
            </option>
          ))}
        </select>
        <input type="date" value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} />
        <input type="text" placeholder="notes (optional)" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <button className="btn" type="submit">Record</button>
      </form>
    </div>
  )
}

function MarkAppliedForm({ job, today, onChanged }: { job: Job; today: string; onChanged: () => void }) {
  const [when, setWhen] = useState(today)
  return (
    <form
      className="outcome-form"
      onSubmit={(e) => {
        e.preventDefault()
        post(`/api/applied/${job.id}`, { when }).then(onChanged)
      }}
    >
      <input type="date" value={when} onChange={(e) => setWhen(e.target.value)} />
      <button className="btn" type="submit" title="You applied on the site yourself.">
        Mark applied
      </button>
    </form>
  )
}

function QueueRow({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const run = (path: string, body?: unknown) => post(path, body).then(onChanged)
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
      </td>
      <td>{job.source}</td>
      <td><VerdictLabel verdict={job.verdict} /></td>
      <td>
        <button className="btn" onClick={() => run(`/api/apply/${job.id}`)} title="Draft this job now.">
          Apply
        </button>
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
  }, [])

  if (!ctx) return null

  const queueRows = ctx.jobs.filter(
    (j) => (j.verdict === 'submit' || j.verdict === 'hold') && !j.terminal_status && !j.has_draft,
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
