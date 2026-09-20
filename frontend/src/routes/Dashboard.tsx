import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { get, type PipelineState, type PipelineStatus, type RunStatusContext, type Verdict } from '../api'
import { describe, type Tone } from '../events'
import { P } from '../icons'
import { Icon, Spinner } from '../components/Icon'
import { TopBarActions, useShell } from '../components/shell'
import { useAction } from '../components/useAction'
import { usePoll } from '../components/usePoll'
import { ago, clock, shortTime } from '../time'
import './Dashboard.css'

// -- shapes /api/overview returns (context.overview_context) --

interface Kpis {
  discovered: number
  after_hard_filter: number
  shortlisted: number
  applied: number
  responses: number
  sparklines: Record<string, string>
}

interface Discovery {
  id: number
  title: string
  company: string
  source: string
  weighted_score: number | null
  location: string | null
  verdict: Verdict | null
}

interface SourceRow {
  source: string
  discovered: number
  pass_rate: number
  applied: number
  response_rate: number
}

interface ScoreDistribution {
  total: number
  high: number
  good: number
  fair: number
  low: number
}

interface RecentOutcome {
  title: string
  company: string
  label: string
  activity_at: string
}

interface OutcomeSummary {
  applied: number
  responses: number
  interviews: number
  offers: number
  callback_rate: number
  interview_rate: number
}

/** EV1: an event row with its job (overview.recent_events). */
interface FeedEvent {
  id: number
  job_id: number | null
  type: string
  payload: string | null
  occurred_at: string
  company: string | null
  title: string | null
  /** 1 within the last 24 h (decided in SQL, naive UTC). */
  recent: number
}

interface OverviewContext extends PipelineStatus {
  brief: { target_titles: string[]; search_locations: string[] }
  daily_cap: number
  today_submitted: number
  kpis: Kpis
  outcome_summary: OutcomeSummary
  source_performance: SourceRow[]
  score_distribution: ScoreDistribution
  recent_discoveries: Discovery[]
  recent_outcomes: RecentOutcome[]
  events: FeedEvent[]
}

const jobName = (e: { company: string | null; title: string | null }) =>
  e.company ? `${e.company}, ${e.title}` : null

// -- tables --

const VERDICT: Record<Verdict, [string, Tone]> = { submit: ['Submit', 'em'], hold: ['Hold', 'am'], skip: ['Skip', 'ro'] }
const SOURCE: Record<string, string> = { linkedin: 'LinkedIn', naukri: 'Naukri', ats: 'Greenhouse' }
const OUTCOME_TONE: Record<string, Tone> = { Interview: 'em', Offer: 'em', Response: 'em', Rejected: 'ro' }

// -- pipeline --

const STAGE_TEXT: Record<string, string> = {
  discover: 'Discovering jobs',
  clean: 'Removing duplicates',
  filter: 'Applying the hard filter',
  score: 'Scoring shortlisted candidates',
  ready: 'Ready',
}

const PIPE_BADGE: Record<PipelineState['status'], [string, Tone]> = {
  running: ['Running', 'sk'],
  error: ['Error', 'ro'],
  idle: ['Idle', 'sl'],
  paused: ['Paused', 'sl'],
  stopped: ['Stopped', 'sl'],
}

type Step = 'done' | 'current' | 'error' | 'todo'

function PipelineCard({ o, refusal }: { o: OverviewContext; refusal: string | null }) {
  const p = o.pipeline_state
  const keys = o.stages.map(([k]) => k)
  const cur = p.stage ? keys.indexOf(p.stage) : -1
  const running = p.status === 'running'
  // An error marks the step it stopped on (the first one if no stage was reached).
  const errAt = p.status === 'error' ? Math.max(cur, 0) : -1
  const allDone = !running && p.status !== 'error' && p.stage === 'ready'
  const step = (i: number): Step =>
    allDone || i < cur ? 'done' : running && i === cur ? 'current' : i === errAt ? 'error' : 'todo'
  const band = 100 / o.stages.length
  const pct = Math.min(
    100,
    cur < 0 ? 0 : p.stage === 'score' && o.max_score ? cur * band + (band * p.scored) / o.max_score : (cur + 1) * band,
  )
  const [badge, tone] = PIPE_BADGE[p.status]
  const counters: [string, number, string?][] = [
    ['Found', p.found],
    ['Duplicates', p.duplicates],
    ['Passed', p.passed],
    ['Scored', p.scored, ` / ${o.max_score}`],
    ['Shortlisted', p.shortlisted],
  ]

  return (
    <section className="card">
      <div className="ch">
        <span className="pipe-title">
          <span className="ct">Pipeline</span>
          <span className={`b ${tone}`}>
            {(running || p.status === 'error') && <span className={`dot dot--${tone}`} />}
            {badge}
          </span>
        </span>
        {p.started_at && <span className="mono pipe-started">started {shortTime(p.started_at)}</span>}
      </div>
      <div className="pipe-body">
        <ol className="stepper">
          {o.stages.map(([key, label], i) => {
            const s = step(i)
            return (
              <li key={key} className={`step step--${s}`} aria-current={s === 'current' ? 'step' : undefined}>
                {i > 0 && <span className="step__line" />}
                <span className="step__dot mono">
                  {s === 'done' ? <Icon d={P.check} size={12} width={3} />
                    : s === 'error' ? <Icon d={P.bang} size={12} width={3} />
                    : i + 1}
                </span>
                <span className="step__label">{label}</span>
              </li>
            )
          })}
        </ol>

        {running && (
          <div className="pipe-progress">
            <div className="pipe-progress__track">
              <div className="pipe-progress__fill" style={{ width: `${pct}%` }} />
            </div>
            <div className="pipe-progress__meta">
              <span>{STAGE_TEXT[p.stage ?? ''] ?? 'Starting'}</span>
              {p.stage === 'score' && <span className="mono">{p.scored} of {o.max_score}</span>}
            </div>
          </div>
        )}

        {p.status === 'error' && p.last_error && (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Run failed:</b> {p.last_error}</span>
          </div>
        )}
        {refusal && (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Refused:</b> {refusal}</span>
          </div>
        )}

        <div className="counters">
          {counters.map(([label, n, of]) => (
            <div className="counter" key={label}>
              <span className="lbl">{label}</span>
              <span className={n ? 'counter__num mono' : 'counter__num mono is-zero'}>
                {n}
                {of && <span className="counter__of">{of}</span>}
              </span>
            </div>
          ))}
        </div>

        {o.feed.length === 0 ? (
          <span className="pipe-none">No activity in this run.</span>
        ) : (
          <div>
            <div className="lbl live-head">Live activity · this run</div>
            {o.feed.map((e, i) => (
              <div className="live" key={i}>
                <span>{e.payload}</span>
                <span className="mono live__time">{clock(e.occurred_at)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

// -- needs you --

function NeedRow({ tone, icon, title, sub, to, action, mono }: {
  tone: Tone
  icon: string
  title: string
  sub: string
  to: string
  action: string
  mono?: boolean
}) {
  return (
    <div className="need">
      <span className={`ico ico--lg ico--${tone}`}><Icon d={icon} /></span>
      <div className="need__text">
        <span className="need__title">{title}</span>
        <span className={mono ? 'need__sub mono' : 'need__sub'}>{sub}</span>
      </div>
      <Link to={to} className="need__link">{action}</Link>
    </div>
  )
}

function NeedsYou({ run, events }: { run: RunStatusContext | null; events: FeedEvent[] }) {
  const card = run?.open_prompt && run.current_job && run.conversation_id ? run : null
  const more = (run?.open_prompt_count ?? 0) - 1
  const resumable = run?.stats.resumable ?? 0
  // ponytail: "recent" failures are the last 24 h of the 20-event feed, so an
  // old one drops off on its own; a real unresolved-failures list needs its own query.
  const failures = events
    .filter((e) => e.recent && (e.type === 'failed_permanent' || e.type === 'held_unknown'))
    .slice(0, 2)
  const empty = !card && !resumable && failures.length === 0

  return (
    <section className="card">
      <div className="ch"><span className="ct">Needs you</span></div>
      {empty ? (
        <div className="empty">
          <span className="empty__icon"><Icon d={P.allClear} size={16} /></span>
          Nothing needs you right now
        </div>
      ) : (
        <div>
          {card && (
            // Interim target: the job's chat. Screen 6 moves it to the job hub's Chat tab.
            <NeedRow tone="am" icon={P.chat} action="Answer" to={`/chat/${card.conversation_id}`}
                     title={`${card.current_job!.company} · ${card.current_job!.title} is waiting on you`}
                     sub={`${card.open_prompt!.question}${more > 0 ? ` · +${more} more` : ''}`} />
          )}
          {resumable > 0 && (
            <NeedRow tone="sl" icon={P.play} action="Review" to="/applications?tab=all&status=interrupted"
                     title={`${resumable} session${resumable === 1 ? '' : 's'} can resume`}
                     sub="Interrupted — continue where they left off" />
          )}
          {failures.map((e) => (
            <NeedRow key={e.id} tone="ro" icon={P.alert} action="Open" mono
                     to={e.job_id ? `/jobs/${e.job_id}`
                       : `/applications?tab=all&status=${e.type === 'held_unknown' ? 'held' : 'failed'}`}
                     title={`${e.company ?? 'A job'} · ${e.title ?? ''} ${e.type === 'held_unknown' ? 'is held' : 'failed'}`}
                     sub={e.payload ?? e.type} />
          ))}
        </div>
      )}
    </section>
  )
}

function Activity({ events }: { events: FeedEvent[] }) {
  return (
    <section className="card">
      <div className="ch"><span className="ct">Activity</span></div>
      {events.length === 0 ? (
        <div className="empty">
          <span className="empty__icon"><Icon d={P.inbox} size={16} /></span>
          No agent activity yet
        </div>
      ) : (
        <div className="feed">
          {events.slice(0, 6).map((e) => {
            const [label, tone, icon] = describe(e)
            const job = jobName(e)
            return (
              <div className="feed__row" key={e.id} title={e.payload ?? undefined}>
                <span className={`ico ico--${tone}`}><Icon d={icon} size={11} width={2.5} /></span>
                <span className="feed__text">
                  {label}
                  {job && <span className="feed__job"> · {job}</span>}
                </span>
                <span className="mono feed__time">{ago(e.occurred_at)}</span>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// -- KPI tiles --

function Tiles({ o, firstRun }: { o: OverviewContext; firstRun: boolean }) {
  const tiles: [string, keyof Kpis, string?][] = [
    ['Discovered', 'discovered'],
    ['After filter', 'after_hard_filter'],
    ['Shortlisted →', 'shortlisted', '/applications'],
    ['Applied', 'applied'],
    ['Responses', 'responses'],
  ]
  const cap = o.daily_cap
  return (
    <div className="tiles">
      {tiles.map(([label, key, to]) => {
        const n = o.kpis[key] as number
        const body = (
          <>
            <span className="lbl">{label}</span>
            <div className="tile__row">
              <span className={n ? 'tile__num mono' : 'tile__num mono is-zero'}>{n}</span>
              {!firstRun && (
                <svg className="tile__spark" viewBox="0 0 64 28" width="56" height="22"
                     preserveAspectRatio="none" aria-hidden="true">
                  <polyline points={o.kpis.sparklines[key]} fill="none" stroke="currentColor"
                            strokeWidth={1.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
                </svg>
              )}
            </div>
          </>
        )
        return to ? (
          <Link key={key} to={to} className="card tile tile--link">{body}</Link>
        ) : (
          <div key={key} className="card tile">{body}</div>
        )
      })}
      <div className="card tile">
        <span className="lbl">Today</span>
        <div className="tile__today">
          <span className={o.today_submitted ? 'tile__num mono' : 'tile__num mono is-zero'}>{o.today_submitted}</span>
          <span className="mono tile__of">/ {cap}</span>
        </div>
        <div className="meter">
          <div className="meter__fill" style={{ width: `${cap ? Math.min(100, (100 * o.today_submitted) / cap) : 0}%` }} />
        </div>
        <span className="tile__note">submitted · daily cap</span>
      </div>
    </div>
  )
}

// -- recent discoveries and the three cards --

function Discoveries({ rows }: { rows: Discovery[] }) {
  return (
    <section className="card table-card">
      <div className="ch">
        <span className="ct">Recent discoveries</span>
        <Link to="/applications" className="ch__link">Open Applications →</Link>
      </div>
      <div className="table-scroll">
        <table className="dash-table">
          <thead>
            <tr>
              {['Verdict', 'Job title', 'Company', 'Source', 'Score', 'Location'].map((h) => (
                <th className="th" key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const v = d.verdict ? VERDICT[d.verdict] : null
              return (
                <tr key={d.id}>
                  <td className="td">{v ? <span className={`b ${v[1]}`}>{v[0]}</span> : <span className="faint">—</span>}</td>
                  <td className="td strong"><Link to={`/jobs/${d.id}`} className="row-link">{d.title}</Link></td>
                  <td className="td muted">{d.company}</td>
                  <td className="td"><span className="src">{SOURCE[d.source] ?? d.source}</span></td>
                  <td className="td mono strong">
                    {d.weighted_score != null ? (
                      <>{Math.round(d.weighted_score)}<span className="score-of">/100</span></>
                    ) : <span className="faint">—</span>}
                  </td>
                  <td className="td loc">{d.location ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Sources({ rows }: { rows: SourceRow[] }) {
  return (
    <section className="card">
      <div className="ch"><span className="ct">Source performance</span></div>
      <table className="mini-table">
        <thead>
          <tr><th>Source</th><th>Found</th><th>Pass</th><th>Applied</th><th>Resp.</th></tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.source}>
              <td>{SOURCE[s.source] ?? s.source}</td>
              <td className="mono">{s.discovered}</td>
              <td className="mono">{Math.round(s.pass_rate)}%</td>
              <td className="mono">{s.applied}</td>
              <td className="mono">{s.applied ? `${Math.round(s.response_rate)}%` : <span className="faint">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

// Neutral slate: the buckets don't map onto verdicts (approved decision).
const BUCKETS: [keyof ScoreDistribution, string, string][] = [
  ['high', '80–100 High', '#475569'],
  ['good', '60–79 Good', '#64748b'],
  ['fair', '40–59 Fair', '#94a3b8'],
  ['low', '0–39 Low', '#cbd5e1'],
]

function Distribution({ d }: { d: ScoreDistribution }) {
  return (
    <section className="card">
      <div className="ch"><span className="ct">Score distribution</span></div>
      <div className="dist">
        {BUCKETS.map(([key, label, color]) => (
          <div key={key} className="dist__row">
            <div className="dist__meta"><span>{label}</span><span className="mono">{Math.round(d[key])}%</span></div>
            <div className="dist__track"><div className="dist__fill" style={{ width: `${d[key]}%`, background: color }} /></div>
          </div>
        ))}
      </div>
    </section>
  )
}

function Outcomes({ s, recent }: { s: OutcomeSummary; recent: RecentOutcome[] }) {
  const stats: [string, string | number][] = [
    ['Applied', s.applied],
    ['Responses', s.responses],
    ['Interviews', s.interviews],
    ['Offers', s.offers],
    ['Callback', `${s.callback_rate}%`],
    ['Interview', `${s.interview_rate}%`],
  ]
  return (
    <section className="card">
      <div className="ch"><span className="ct">Outcomes this month</span></div>
      <div className="outcome-stats">
        {stats.map(([label, v]) => (
          <div key={label}><span>{label}</span><span className="mono">{v}</span></div>
        ))}
      </div>
      <div className="outcomes">
        {recent.length === 0 ? (
          <span className="pipe-none">No applications submitted yet.</span>
        ) : (
          recent.slice(0, 3).map((r, i) => (
            <div className="outcome" key={i}>
              <span className="outcome__job">{r.company} · {r.title}</span>
              <span className="outcome__meta">
                <span className={`b ${OUTCOME_TONE[r.label] ?? 'sl'}`}>{r.label}</span>
                <span className="mono">{r.activity_at.slice(5, 10)}</span>
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

// -- first run --

function Check({ state, children, action }: { state: 'done' | 'warn' | 'todo'; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`check check--${state}`}>
      <span className="check__dot">
        {state === 'done' && <Icon d={P.check} size={12} width={3} />}
        {state === 'warn' && <Icon d={P.bang} size={12} width={3} />}
      </span>
      <span className="check__text">{children}</span>
      {action}
    </div>
  )
}

function FirstRun({ o, canRun, onRun }: { o: OverviewContext; canRun: boolean; onRun: () => void }) {
  const [facts, setFacts] = useState<{ n: number; min: number } | null>(null)
  const [master, setMaster] = useState<boolean | null>(null)

  // Mounted only while nothing is scored, so these one-shot reads never poll.
  useEffect(() => {
    get<{ items: unknown[]; min_hard: number }>('/api/facts')
      .then((f) => setFacts({ n: f.items.length, min: f.min_hard }))
      .catch(() => {})
    get<{ master: { exists: boolean } }>('/api/resumes')
      .then((r) => setMaster(r.master.exists))
      .catch(() => {})
  }, [])

  const titles = o.brief.target_titles.length
  const places = o.brief.search_locations.length
  return (
    <section className="card">
      <div className="ch ch--stack">
        <span className="ct">Get your first shortlist</span>
        <span className="ch__sub">Four things before the agent can score and apply.</span>
      </div>
      <Check state="done" action={<Link to="/settings" className="check__link">Edit in Settings</Link>}>
        Career brief loaded — {titles} target title{titles === 1 ? '' : 's'}, {places} location{places === 1 ? '' : 's'}
      </Check>
      {facts === null ? (
        <Check state="todo">Checking facts…</Check>
      ) : facts.n < facts.min ? (
        <Check state="warn" action={<Link to="/facts" className="btn">Add facts</Link>}>
          <b>Facts: <span className="mono">{facts.n} of {facts.min}</span></b> — scoring is blocked below {facts.min}
        </Check>
      ) : (
        <Check state="done" action={<Link to="/facts" className="check__link">Facts</Link>}>
          Facts: <span className="mono">{facts.n}</span> — enough to score
        </Check>
      )}
      {master === null ? (
        <Check state="todo">Checking the master resume…</Check>
      ) : master ? (
        <Check state="done" action={<Link to="/resumes" className="check__link">View</Link>}>
          Master resume uploaded
        </Check>
      ) : (
        <Check state="warn" action={<Link to="/resumes" className="btn">Upload</Link>}>
          Upload your master resume — tailoring needs it
        </Check>
      )}
      <Check state="todo" action={
        <button type="button" className="btn" disabled={!canRun} onClick={onRun}>
          <span className="play-muted"><Icon d={P.play} size={12} fill /></span>Run pipeline
        </button>
      }>
        Run the pipeline to discover and score jobs
      </Check>
    </section>
  )
}

// -- page --

function Skeleton() {
  return (
    <div className="tiles-and-rows" aria-busy="true">
      <div className="tiles">
        {Array.from({ length: 6 }, (_, i) => (
          <div className="card tile" key={i}>
            <span className="skel" style={{ width: '55%' }} />
            <span className="skel skel--tall" style={{ width: '35%' }} />
          </div>
        ))}
      </div>
      <div className="dash-row">
        {[0, 1].map((i) => (
          <div className="card skel-card" key={i}>
            {[70, 90, 60, 80].map((w) => <span className="skel" key={w} style={{ width: `${w}%` }} />)}
          </div>
        ))}
      </div>
    </div>
  )
}

/** Dashboard (approved Screen 2): how the search is performing and whether
 * anything needs you. One /api/overview poll every 3 s; run status comes from
 * the shell's shared poll. */
export function Dashboard() {
  const { data: o, error, reload } = usePoll<OverviewContext>('/api/overview')
  const { run, toast } = useShell()
  const { busy, error: refusal, run: act } = useAction(reload)

  const status = o?.pipeline_state.status
  const running = status === 'running' || busy
  const canRun = (status === 'idle' || status === 'error') && !busy
  const firstRun = o?.score_distribution.total === 0

  const runPipeline = () =>
    act('/api/pipeline/run-now').then((r) => {
      if (!r || !o) return
      toast({
        text: o.max_score
          ? `Pipeline queued — scoring up to ${o.max_score} jobs`
          : 'Pipeline queued — discovery and the hard filter only',
      })
    })

  return (
    <div className="page">
      <TopBarActions>
        <button type="button" className="btn pri" disabled={!canRun} onClick={runPipeline}>
          {running ? <Spinner /> : <Icon d={P.play} size={14} fill />}
          {running ? 'Pipeline running…' : 'Run pipeline'}
        </button>
      </TopBarActions>

      <div className="page-head">
        <h1>Dashboard</h1>
        <p>How your search is performing</p>
      </div>

      {!o ? (
        error ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load the dashboard:</b> {error}</span>
          </div>
        ) : (
          <Skeleton />
        )
      ) : (
        <>
          <Tiles o={o} firstRun={firstRun} />
          <div className="dash-row">
            <PipelineCard o={o} refusal={refusal} />
            <div className="dash-side">
              <NeedsYou run={run} events={o.events} />
              <Activity events={o.events} />
            </div>
          </div>
          {firstRun ? (
            <FirstRun o={o} canRun={canRun} onRun={runPipeline} />
          ) : (
            <>
              <Discoveries rows={o.recent_discoveries} />
              <div className="dash-cards">
                <Sources rows={o.source_performance} />
                <Distribution d={o.score_distribution} />
                <Outcomes s={o.outcome_summary} recent={o.recent_outcomes} />
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
