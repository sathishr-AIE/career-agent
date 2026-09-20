import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { get, type PipelineStatus, type RunState, type RunStatusContext } from '../api'
import { shortTime } from '../time'
import { useShell } from './shell'
import { usePoll } from './usePoll'
import './Sidebar.css'

// Icon paths copied verbatim from the approved Sidebar artboard
// (docs/design/frontend-redesign/source/Sidebar.dc.html).
const ICONS = {
  home: 'M3 11l9-7 9 7M5 10v10h5v-6h4v6h5V10',
  dashboard: 'M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z',
  applications: 'M4 7h16v12H4zM9 7V5h6v2M4 12h16',
  profile: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20c1.5-4 4.5-6 8-6s6.5 2 8 6',
  facts: 'M4 6l1.5 1.5L8 5M4 12l1.5 1.5L8 11M4 18l1.5 1.5L8 17M11 6h9M11 12h9M11 18h9',
  resumes: 'M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6',
  memory: 'M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4',
  logins: 'M7.5 16.5a4 4 0 1 1 0-8 4 4 0 0 1 0 8zM11.5 12.5H21M17 12.5V16M20 12.5V15',
  settings: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M16 4v4M10 10v4M18 16v4',
}

interface NavItem {
  key: keyof typeof ICONS
  to: string
  label: string
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: 'Workspace',
    items: [
      { key: 'home', to: '/', label: 'Home' },
      { key: 'dashboard', to: '/dashboard', label: 'Dashboard' },
      { key: 'applications', to: '/applications', label: 'Applications' },
    ],
  },
  {
    group: 'Candidate',
    items: [
      { key: 'profile', to: '/profile', label: 'Profile' },
      { key: 'facts', to: '/facts', label: 'Facts' },
      { key: 'resumes', to: '/resumes', label: 'Resumes' },
    ],
  },
  {
    group: 'Agent',
    items: [
      { key: 'memory', to: '/memory', label: 'Memory' },
      { key: 'logins', to: '/logins', label: 'Logins' },
      { key: 'settings', to: '/settings', label: 'Settings' },
    ],
  },
]

/** The nav item (and its group) a path belongs to -- also the breadcrumb.
 * A job chat (/chat/:id) sits under Home until Screen 6 moves it into the job
 * hub; the job hub (/jobs/:id) sits under Applications. */
export function navItemFor(pathname: string): (NavItem & { group: string }) | undefined {
  for (const g of NAV) {
    for (const it of g.items) {
      const hit =
        it.to === '/'
          ? pathname === '/'
          : pathname === it.to || pathname.startsWith(`${it.to}/`)
            || (it.key === 'applications' && pathname.startsWith('/jobs/'))
      if (hit) return { ...it, group: g.group }
    }
  }
  return undefined
}

type Status = RunState['status']
const LABEL: Record<Status, string> = {
  idle: 'Idle',
  running: 'Running',
  paused: 'Paused',
  stopped: 'Stopped',
  error: 'Error',
}
// Colour means status only: sky while running, rose on error, slate otherwise.
const TONE: Record<Status, string> = { idle: 'sl', running: 'sk', paused: 'sl', stopped: 'sl', error: 'ro' }

function Icon({ d }: { d: string }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

/** True while the fact count is below min_warn (the Facts amber dot). Facts
 * only change on the Facts page, so this re-checks on navigation, not on a timer. */
function useFactsLow(pathname: string) {
  const [low, setLow] = useState(false)
  useEffect(() => {
    get<{ items: unknown[]; min_warn: number }>('/api/facts')
      .then((f) => setLow(f.items.length < f.min_warn))
      .catch(() => {
        /* leave the dot as it was */
      })
  }, [pathname])
  return low
}

function pipelineNote({ pipeline_state: p, stages, max_score }: PipelineStatus): string {
  if (p.status === 'running') {
    if (p.stage === 'score') return `Score ${p.scored}/${max_score}`
    return stages.find(([k]) => k === p.stage)?.[1] ?? ''
  }
  if (!p.started_at) return ''
  return p.status === 'error' ? shortTime(p.started_at) : `last run ${shortTime(p.started_at)}`
}

function StatusCard({ run, pipe, answerTo }: {
  run: RunStatusContext | null
  pipe: PipelineStatus | null
  answerTo: string | null
}) {
  if (!run) {
    return (
      <div className="status-card" aria-busy="true">
        <span className="skel" style={{ width: '70%' }} />
        <span className="skel" style={{ width: '45%' }} />
      </div>
    )
  }
  const a = run.run_state
  const cards = run.open_prompt_count
  return (
    <div className="status-card">
      <div className="status-card__row">
        <span className="status-card__title">
          <span className={`dot dot--lg dot--${TONE[a.status]}${a.status === 'running' ? ' dot--live' : ''}`} />
          Apply run · {LABEL[a.status]}
        </span>
        {a.mode && (a.status === 'running' || a.status === 'paused') && (
          <span className="status-card__mode mono">{a.mode}</span>
        )}
      </div>
      {run.current_job && (
        <div className="status-card__job">
          {run.current_job.company} — {run.current_job.title}
        </div>
      )}
      {pipe && (
        <div className="status-card__row status-card__meta">
          <span className="status-card__with-dot">
            <span className={`dot dot--${TONE[pipe.pipeline_state.status]}`} />
            Pipeline · {LABEL[pipe.pipeline_state.status]}
          </span>
          <span className="status-card__note mono">{pipelineNote(pipe)}</span>
        </div>
      )}
      {cards > 0 && (
        <div className="status-card__cards">
          <span className="status-card__with-dot">
            <span className="dot dot--am" />
            {cards} card{cards === 1 ? '' : 's'} waiting
          </span>
          {answerTo && <Link to={answerTo}>Answer →</Link>}
        </div>
      )}
      {!run.submission_implemented && (
        <div className="status-card__kill" title="Real submission is disabled; every apply stops at a draft.">
          <span className="status-card__with-dot">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                 strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" />
            </svg>
            Submission off
          </span>
          <span className="status-card__kill-tag mono">Draft only</span>
        </div>
      )}
    </div>
  )
}

/** The workspace sidebar (approved Screen 1): nav groups plus the agent status
 * card, which replaces the old bottom AgentStatusBar. */
export function Sidebar() {
  const { pathname } = useLocation()
  const { run } = useShell()
  const pipe = usePoll<PipelineStatus>('/api/pipeline/status').data
  const factsLow = useFactsLow(pathname)
  const here = navItemFor(pathname)?.key
  const queued = run?.stats.queued ?? 0
  const cards = run?.open_prompt_count ?? 0
  const answerTo = run?.open_prompt && run.current_job
    ? `/jobs/${run.current_job.job_id}/chat` : null

  return (
    <aside className="sidebar">
      <div>
        <div className="brand">
          <span className="brand__mark">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                 strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M13 3L5 13h6l-1 8 8-10h-6l1-8z" />
            </svg>
          </span>
          <span className="brand__text">
            <span className="brand__name">Career Agent</span>
            <span className="brand__tag">Quality-Gated Engine</span>
          </span>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV.map((g) => (
            <div key={g.group}>
              <div className="nav__label">{g.group}</div>
              <div className="nav__items">
                {g.items.map((it) => {
                  const active = it.key === here
                  return (
                    <Link key={it.key} to={it.to} title={it.label}
                          className={active ? 'nav-item active' : 'nav-item'}
                          aria-current={active ? 'page' : undefined}>
                      <span className="nav-item__main">
                        <Icon d={ICONS[it.key]} />
                        <span className="nav-item__label">{it.label}</span>
                      </span>
                      {it.key === 'applications' && queued > 0 && (
                        <span className="nav-count mono">
                          {queued}
                          <span className="sr-only"> queued</span>
                        </span>
                      )}
                      {it.key === 'facts' && factsLow && (
                        <span className="nav-dot" role="img" aria-label="Fewer facts than recommended" />
                      )}
                    </Link>
                  )
                })}
              </div>
            </div>
          ))}
        </nav>
      </div>
      <div className="sidebar__foot">
        <StatusCard run={run} pipe={pipe} answerTo={answerTo} />
        {run && (
          <div className="status-compact">
            <span className={`dot dot--lg dot--${TONE[run.run_state.status]}`}
                  title={`Apply run · ${LABEL[run.run_state.status]}`} />
            {cards > 0 &&
              (answerTo ? (
                <Link to={answerTo} className="status-compact__cards" title={`${cards} waiting · Answer`}>
                  {cards}
                </Link>
              ) : (
                <span className="status-compact__cards" title={`${cards} waiting`}>{cards}</span>
              ))}
          </div>
        )}
      </div>
    </aside>
  )
}
