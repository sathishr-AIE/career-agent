import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post, type Verdict } from '../api'
import { VerdictRail } from '../components/VerdictRail'
import '../components/ui.css'
import './Dashboard.css'

interface Kpis {
  discovered: number
  after_hard_filter: number
  shortlisted: number
  applied: number
  responses: number
  sparklines: Record<string, string>
}

interface Discovery {
  title: string
  company: string
  source: string
  weighted_score: number | null
  location: string | null
  gate: string
  verdict: Verdict | null
}

interface SourceRow {
  source: string
  discovered: number
  pass_rate: number
  shortlist_rate: number
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

interface PipelineState {
  status: 'idle' | 'running' | 'paused' | 'stopped' | 'error'
  last_error: string | null
  started_at: string | null
  stage: string | null
  found: number
  duplicates: number
  passed: number
  scored: number
  shortlisted: number
}

interface PipelineStatus {
  pipeline_state: PipelineState
  feed: { payload: string; occurred_at: string }[]
  stages: [string, string][]
  max_score: number
}

interface OverviewContext {
  kpis: Kpis
  outcome_summary: OutcomeSummary
  source_performance: SourceRow[]
  score_distribution: ScoreDistribution
  recent_discoveries: Discovery[]
  recent_outcomes: RecentOutcome[]
  shortlisted_count: number
  pipeline_state: PipelineState
  feed: PipelineStatus['feed']
  stages: PipelineStatus['stages']
  max_score: number
}

function Sparkline({ points }: { points: string }) {
  return (
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points={points} fill="none" stroke="var(--ink-dim)" strokeWidth={2} />
    </svg>
  )
}

function PipelinePanel({
  status,
  onRunNow,
}: {
  status: PipelineStatus
  onRunNow: () => void
}) {
  const s = status.pipeline_state
  const stageKeys = status.stages.map(([k]) => k)
  const idx = s.stage ? stageKeys.indexOf(s.stage) : -1
  const band = 100 / status.stages.length
  const inScore = s.stage === 'score' && status.max_score
  const pct = Math.min(
    100,
    idx < 0 ? 0 : inScore ? idx * band + (band * s.scored) / status.max_score : (idx + 1) * band,
  )
  const canRun = s.status === 'idle' || s.status === 'error'

  return (
    <div className="card">
      <h2>Pipeline</h2>
      <div style={{ marginBottom: 'var(--space-4)' }}>
        <button className="btn primary" disabled={!canRun} onClick={onRunNow}>
          {canRun ? '▶ Run Now' : '◌ Running…'}
        </button>{' '}
        <span className="rationale">{s.status}</span>
        {s.last_error && <div className="field-error">{s.last_error}</div>}
      </div>

      <div className="stage-track">
        {status.stages.map(([key, label], i) => (
          <div key={key} className={`stage ${i < idx ? 'done' : i === idx ? 'active' : ''}`}>
            {label}
          </div>
        ))}
      </div>
      <div className="progress-line">
        <div className="progress-bar" style={{ width: `${pct}%` }} />
      </div>

      <div className="counters">
        <div>
          <div className="counter-top">Jobs Found</div>
          <div className="counter-num">{s.found}</div>
        </div>
        <div>
          <div className="counter-top">Passed Filter</div>
          <div className="counter-num">{s.passed}</div>
        </div>
        <div>
          <div className="counter-top">AI Scored</div>
          <div className="counter-num">
            {s.scored} / {status.max_score}
          </div>
        </div>
        <div>
          <div className="counter-top">Shortlisted</div>
          <div className="counter-num">{s.shortlisted}</div>
        </div>
      </div>

      <h3 style={{ fontSize: '0.9rem' }}>Live activity</h3>
      <div className="activity">
        {status.feed.length === 0 ? (
          <p className="rationale">No activity yet.</p>
        ) : (
          status.feed.map((e, i) => (
            <div className="activity-item" key={i}>
              <span>{e.payload}</span>
              <span className="activity-time">{e.occurred_at}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

export function Dashboard() {
  const [data, setData] = useState<OverviewContext | null>(null)

  const reload = () => get<OverviewContext>('/api/overview').then(setData)

  useEffect(() => {
    reload()
    const id = setInterval(reload, 3000)
    return () => clearInterval(id)
  }, [])

  if (!data) return null
  const { kpis, outcome_summary, source_performance, score_distribution,
    recent_discoveries, recent_outcomes, shortlisted_count } = data
  const pipeline: PipelineStatus = {
    pipeline_state: data.pipeline_state,
    feed: data.feed,
    stages: data.stages,
    max_score: data.max_score,
  }

  const distBuckets: { key: keyof ScoreDistribution; label: string; verdict: Verdict }[] = [
    { key: 'high', label: '80–100 High', verdict: 'submit' },
    { key: 'good', label: '60–79 Good', verdict: 'submit' },
    { key: 'fair', label: '40–59 Fair', verdict: 'hold' },
    { key: 'low', label: '0–39 Low', verdict: 'skip' },
  ]

  return (
    <>
      <h1 className="page-title">Dashboard</h1>

      <div className="kpis">
        {(['discovered', 'after_hard_filter', 'shortlisted', 'applied', 'responses'] as const).map(
          (key) => (
            <div className="card kpi" key={key}>
              <h3>{key.replace(/_/g, ' ')}</h3>
              <div className="num">{kpis[key]}</div>
              <Sparkline points={kpis.sparklines[key]} />
            </div>
          ),
        )}
      </div>

      <PipelinePanel status={pipeline} onRunNow={() => post('/api/pipeline/run-now').then(reload)} />

      <div className="card">
        <h2>Recent Discoveries</h2>
        <table>
          <thead>
            <tr>
              <th></th>
              <th>Job Title</th>
              <th>Company</th>
              <th>Source</th>
              <th>Score</th>
              <th>Location</th>
            </tr>
          </thead>
          <tbody>
            {recent_discoveries.map((d, i) => (
              <tr key={i}>
                <td style={{ width: 8 }}>
                  <div style={{ height: '1.5rem' }}>
                    <VerdictRail verdict={d.verdict} fraction={(d.weighted_score ?? 0) / 100} />
                  </div>
                </td>
                <td>
                  <b>{d.title}</b>
                </td>
                <td>{d.company}</td>
                <td>{d.source}</td>
                <td className="data">{d.weighted_score != null ? Math.round(d.weighted_score) : '—'}</td>
                <td>{d.location ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="bottom-grid">
        <div className="card">
          <h2>Source Performance</h2>
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Discovered</th>
                <th>Pass</th>
                <th>Shortlist</th>
                <th>Applied</th>
                <th>Response</th>
              </tr>
            </thead>
            <tbody>
              {source_performance.map((s) => (
                <tr key={s.source}>
                  <td>
                    <b>{s.source}</b>
                  </td>
                  <td className="data">{s.discovered}</td>
                  <td className="data">{s.pass_rate}%</td>
                  <td className="data">{s.shortlist_rate}%</td>
                  <td className="data">{s.applied}</td>
                  <td className="data">{s.response_rate}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>Score Distribution</h2>
          {score_distribution.total === 0 ? (
            <p className="rationale">No scored jobs yet.</p>
          ) : (
            <div className="dist-bars">
              {distBuckets.map((b) => (
                <div className="dist-bar" key={b.key}>
                  <div className="dist-bar-track">
                    <VerdictRail verdict={b.verdict} fraction={score_distribution[b.key] / 100} size="wide" />
                  </div>
                  <div className="dist-bar-label">
                    {b.label}
                    <br />
                    {score_distribution[b.key]}%
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="bottom-grid" style={{ marginTop: 'var(--space-6)' }}>
        <div className="card">
          <h2>Recent Outcomes</h2>
          {recent_outcomes.length === 0 ? (
            <p className="rationale">No applications submitted yet.</p>
          ) : (
            recent_outcomes.map((o, i) => (
              <div className="recent-item" key={i}>
                <div>
                  <b>{o.title}</b>
                  <br />
                  <span className="rationale">{o.company}</span>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <span className="pill">{o.label}</span>
                  <div className="rationale data">{o.activity_at}</div>
                </div>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <h2>Outcome Summary (This Month)</h2>
          <div className="kv">
            <div>
              <span>Applied</span>
              <b>{outcome_summary.applied}</b>
            </div>
            <div>
              <span>Responses</span>
              <b>{outcome_summary.responses}</b>
            </div>
            <div>
              <span>Interviews</span>
              <b>{outcome_summary.interviews}</b>
            </div>
            <div>
              <span>Offers</span>
              <b>{outcome_summary.offers}</b>
            </div>
            <div>
              <span>Callback Rate</span>
              <b>{outcome_summary.callback_rate}%</b>
            </div>
            <div>
              <span>Interview Rate</span>
              <b>{outcome_summary.interview_rate}%</b>
            </div>
          </div>
        </div>
      </div>

      <div className="card cta" style={{ marginTop: 'var(--space-6)' }}>
        <div>
          <h3 style={{ marginBottom: 4 }}>Ready to apply?</h3>
          <p className="rationale">
            You have <b>{shortlisted_count} shortlisted jobs</b>. Review and apply from the
            Applications page.
          </p>
        </div>
        <Link className="btn primary" to="/applications">
          View Shortlisted Jobs →
        </Link>
      </div>
    </>
  )
}
