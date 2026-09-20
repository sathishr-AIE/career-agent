import { useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, Outlet, useLocation, useParams } from 'react-router-dom'
import type { Job as JobRow } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Icon, Spinner } from '../components/Icon'
import { StatusBadge } from '../components/JobParts'
import { Menu, type MenuItem } from '../components/Menu'
import { useCrumb } from '../components/shell'
import { useJobActions, type JobActions } from '../components/useJobActions'
import { usePoll } from '../components/usePoll'
import { P } from '../icons'
import { applyPath, SOURCE, stateOf, VERDICT, type State } from '../jobState'
import './Job.css'

// -- JD1: GET /api/jobs/{id} (context.job_detail_context) --

export interface Assessment {
  stage: 'hard' | 'scored'
  verdict: 'submit' | 'hold' | 'skip'
  rationale: string
  role_fit: number | null
  credibility: number | null
  opportunity: number | null
  application_quality: number | null
  eligibility_soft: number | null
  weighted_score: number | null
  model: string
  prompt_version: string
  created_at: string
}

export interface Attempt {
  id: number
  status: 'draft' | 'in_flight' | 'submitted' | 'failed' | 'failed_permanent' | 'held_unknown'
  failure_reason: string | null
  resume_version: string
  started_at: string | null
  submitted_at: string | null
  has_transcript: boolean
  outcomes: { type: string; derived: number; occurred_at: string; notes: string | null }[]
  effective_outcome: string | null
}

export interface JobDetail {
  job: {
    id: number
    company: string
    title: string
    location: string | null
    is_remote: number
    comp_min: number | null
    comp_max: number | null
    posted_at: string | null
    url: string | null
    source: string
    description: string | null
    discovered_at: string
    merged_into_job_id: number | null
    dismissed_at: string | null
  }
  merged_into: { id: number; company: string; title: string } | null
  /** The job's Applications row: the same state rules as that page. */
  row: JobRow | null
  assessments: Assessment[]
  applications: Attempt[]
  checkpoint: {
    status: 'running' | 'waiting' | 'resumable' | 'done'
    step: string
    mode: string | null
    /** The model this session was pinned to (MS1): the Chat tab's picker
     * locks to it while the session runs. */
    model: string | null
    resume_count: number
    max_resumes: number
    form_url: string | null
    updated_at: string
    notes_count: number
  } | null
  resume: {
    version: string
    summary: string
    bullets: { text: string; fact_ids: number[]; fact_claims: (string | null)[] }[]
  } | null
  conversation_id: number | null
  open_prompt: { id: number; kind: string; question: string } | null
  events: { type: string; payload: string | null; occurred_at: string }[]
  gate_threshold: number
  manual_types: string[]
  outcome_labels: Record<string, string>
  today: string
}

/** Which form the Applications card has open (a header primary opens one). */
export type HubForm = 'outcome' | 'submitted' | 'applied' | null

export interface HubContext {
  d: JobDetail
  state: State | null
  actions: JobActions
  form: HubForm
  setForm: (f: HubForm) => void
  askClearHold: () => void
}

/** INR/year as lakhs: "₹38–46 L", "₹38 L+", "up to ₹46 L". */
function comp(min: number | null, max: number | null): string | null {
  const l = (n: number) => {
    const v = n / 1e5
    return Number.isInteger(v) ? String(v) : v.toFixed(1)
  }
  if (min && max) return `₹${l(min)}–${l(max)} L`
  if (min) return `₹${l(min)} L+`
  if (max) return `up to ₹${l(max)} L`
  return null
}

function HubHeader({ d, state, actions, setForm, askClearHold }: Omit<HubContext, 'form'>) {
  const { job, row } = d
  const latest = d.assessments[0]
  const v = latest ? VERDICT[latest.verdict] : null
  const hard = row?.stage === 'hard' || latest?.stage === 'hard'
  const pay = comp(job.comp_min, job.comp_max)
  const meta = [
    job.location,
    pay && <span className="mono" key="pay">{pay}</span>,
    SOURCE[job.source] ?? job.source,
    job.posted_at && <span key="posted">posted <span className="mono">{job.posted_at.slice(0, 10)}</span></span>,
  ].filter(Boolean)

  const primary = (label: string, onClick: () => void, icon?: string) => (
    <button type="button" className="btn pri" disabled={actions.busy} onClick={onClick}>
      {actions.busy ? <Spinner /> : icon && <Icon d={icon} size={14} fill />}
      {label}
    </button>
  )
  let main: ReactNode = null
  let items: (MenuItem | 'sep')[] = []
  let note: string | undefined
  const dismiss: MenuItem = {
    label: 'Dismiss', icon: P.x, danger: true,
    onSelect: () => actions.act(`/api/dismiss/${job.id}`, undefined, 'Dismissed'),
  }
  const markApplied: MenuItem = { label: 'Mark applied…', icon: P.check, onSelect: () => setForm('applied') }
  if (row && state && !hard) {
    switch (state) {
      case 'queued':
        main = primary(row.verdict === 'skip' ? 'Track anyway' : 'Apply', () => actions.startRun(applyPath(row)), P.play)
        if (row.verdict !== 'skip') {
          items = [
            { label: 'Move up', icon: P.up,
              onSelect: () => actions.act(`/api/queue/${job.id}/priority`, { direction: 'up' }, 'Moved up') },
            { label: 'Move down', icon: P.down,
              onSelect: () => actions.act(`/api/queue/${job.id}/priority`, { direction: 'down' }, 'Moved down') },
            markApplied,
            'sep',
            { label: 'Skip this job', icon: P.arrowRight, danger: true,
              onSelect: () => actions.act(`/api/queue/${job.id}/skip`, undefined, 'Skipped — moved out of the queue') },
          ]
        }
        break
      case 'drafted':
        main = primary('Open draft', () => actions.openChat())
        items = [
          { label: 'Redo draft', icon: P.undo, onSelect: () => actions.startRun(applyPath(row)) },
          markApplied, 'sep', dismiss,
        ]
        break
      case 'in_progress':
        main = primary('Open chat', () => actions.openChat())
        break
      case 'interrupted':
        main = primary('Continue where it left off', actions.resume, P.play)
        break
      case 'held':
        main = primary('It was submitted', () => setForm('submitted'))
        note = 'The agent may already have submitted this. Check the employer’s site first.'
        items = [
          ...(row.application_id ? [{
            label: 'View transcript', icon: P.transcript,
            onSelect: () => window.open(`/api/transcript/${row.application_id}`, '_blank', 'noreferrer'),
          }] : []),
          'sep',
          { label: 'Not submitted — clear hold…', icon: P.undo, danger: true, onSelect: askClearHold },
        ]
        break
      case 'submitted':
        main = primary('Record outcome', () => setForm('outcome'))
        break
      case 'dismissed':
        main = primary('Undo', () => actions.act(`/api/queue/${job.id}/restore`, undefined, 'Back in the queue'))
        break
      case 'failed':
        main = row.application_id ? (
          <a className="btn pri" href={`/api/transcript/${row.application_id}`} target="_blank" rel="noreferrer">
            Transcript
          </a>
        ) : null
        items = [dismiss]
        break
    }
  }

  return (
    <div className="hub-head">
      <div className="hub-head__inner">
        <div className="hub-head__top">
          <div className="hub-id">
            <span className="hub-logo" aria-hidden="true">{job.company.charAt(0).toUpperCase()}</span>
            <div className="hub-id__text">
              <div className="hub-title">
                <h1>{job.title}</h1>
                {v && <span className={`b ${v[1]}`}>{v[0]}</span>}
                {latest?.weighted_score != null && (
                  <span className="mono hub-score">{Math.round(latest.weighted_score)}<span>/100</span></span>
                )}
                {row && state && !hard && (
                  <StatusBadge state={state} reason={row.failure_reason} outcomeLabels={d.outcome_labels}
                               waiting={!!d.open_prompt && state === 'in_progress'}
                               outcome={d.applications.find((a) => a.status === 'submitted')?.effective_outcome} />
                )}
              </div>
              <div className="hub-meta">
                <span className="hub-meta__company">{job.company}</span>
                {job.is_remote ? <span className="b sl">Remote</span> : null}
                {meta.map((m, i) => (
                  <span className="hub-meta__item" key={i}><span aria-hidden="true">·</span>{m}</span>
                ))}
                {job.url && (
                  <span className="hub-meta__item">
                    <span aria-hidden="true">·</span>
                    <a href={job.url} target="_blank" rel="noreferrer" className="hub-meta__link">View posting ↗</a>
                  </span>
                )}
              </div>
            </div>
          </div>
          {(main || items.length > 0) && (
            <div className="hub-actions">
              {main}
              {items.length > 0 && <span className="hub-menu"><Menu items={items} note={note} /></span>}
            </div>
          )}
        </div>
        {actions.error && (
          <div className="alert" role="alert"><Icon d={P.refused} size={16} /><span><b>Refused:</b> {actions.error}</span></div>
        )}
        <nav className="hub-tabs" aria-label="Job">
          <NavLink end to={`/jobs/${job.id}`} className="hub-tab">Details</NavLink>
          <NavLink to={`/jobs/${job.id}/chat`} className="hub-tab">
            Chat{d.open_prompt && <span className="dot dot--am" role="img" aria-label="A card is waiting" />}
          </NavLink>
        </nav>
      </div>
    </div>
  )
}

function HubSkeleton() {
  return (
    <div aria-busy="true">
      <div className="hub-head">
        <div className="hub-head__inner">
          <div className="hub-id">
            <span className="hub-logo" />
            <div className="hub-id__text">
              <span className="skel" style={{ width: 320, height: 18 }} />
              <span className="skel" style={{ width: 460 }} />
            </div>
          </div>
          <div className="hub-tabs"><span className="skel" style={{ width: 90, margin: '14px 0' }} /></div>
        </div>
      </div>
      <div className="hub-cols">
        <div className="hub-main">
          {[180, 220].map((h) => <div className="card" key={h} style={{ height: h }} />)}
        </div>
        <div className="hub-side"><div className="card" style={{ height: 160 }} /></div>
      </div>
    </div>
  )
}

/** The job hub (approved Screen 4): a header shared by the Details and Chat
 * tabs, over the tab's body. Polls JD1 every 3 s only while a run is live,
 * and otherwise when the window regains focus. */
export function Job() {
  const jobId = Number(useParams().id)
  // The Chat tab is a fixed-height column (its transcript scrolls itself);
  // Details scrolls with the page.
  const chatTab = useLocation().pathname.endsWith('/chat')
  const [live, setLive] = useState(false)
  const { data: d, error, status, reload } = usePoll<JobDetail>(`/api/jobs/${jobId}`, live ? 3000 : 0)
  const nowLive = !!d && (d.applications.some((a) => a.status === 'in_flight')
    || d.checkpoint?.status === 'running' || d.checkpoint?.status === 'waiting')
  if (nowLive !== live) setLive(nowLive)   // derived from the last payload
  const actions = useJobActions(jobId, reload)
  const [form, setForm] = useState<HubForm>(null)
  const [confirming, setConfirming] = useState(false)
  useCrumb(d ? `${d.job.company} · ${d.job.title}` : null)

  useEffect(() => {
    window.addEventListener('focus', reload)
    return () => window.removeEventListener('focus', reload)
  }, [reload])

  if (!d) {
    if (status === 404) {
      return (
        <div className="card">
          <div className="empty">
            <span className="empty__icon"><Icon d={P.inbox} size={16} /></span>
            This job no longer exists
            <Link to="/applications" className="btn">Back to Applications</Link>
          </div>
        </div>
      )
    }
    return error ? (
      <div className="alert" role="alert"><Icon d={P.refused} size={16} /><span><b>Can't load this job:</b> {error}</span></div>
    ) : (
      <HubSkeleton />
    )
  }

  const state = d.row ? stateOf(d.row) : null
  const hub: HubContext = { d, state, actions, form, setForm, askClearHold: () => setConfirming(true) }
  return (
    <div className={chatTab ? 'hub hub--chat' : 'hub'}>
      <HubHeader {...hub} />
      <Outlet context={hub} />
      {confirming && (
        <ConfirmDialog
          title="Clear the hold?"
          text="Confirm this application was NOT submitted. Clearing the hold lets the agent apply to this job again."
          confirm="Not submitted — clear hold"
          busy={actions.busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false)
            actions.act(`/api/queue/${d.job.id}/retry?confirm=1`, undefined, 'Hold cleared — requeued')
          }}
        />
      )}
    </div>
  )
}
