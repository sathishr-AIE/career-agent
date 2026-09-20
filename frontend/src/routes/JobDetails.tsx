import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { Icon } from '../components/Icon'
import { AppliedForm, OutcomeForm, ScoreBars } from '../components/JobParts'
import { describe, type Tone } from '../events'
import { P } from '../icons'
import { OUTCOME_TONE, SOURCE, VERDICT } from '../jobState'
import { dateTime, stamp } from '../time'
import type { Attempt, HubContext } from './Job'

const ATTEMPT_BADGE: Record<Attempt['status'], [string, Tone]> = {
  draft: ['Draft', 'sl'],
  in_flight: ['In progress', 'sk'],
  submitted: ['Submitted', 'em'],
  failed: ['Failed', 'ro'],
  failed_permanent: ['Failed permanently', 'ro'],
  held_unknown: ['Held', 'am'],
}
const SESSION_BADGE: Record<string, [string, Tone]> = {
  running: ['Running', 'sk'],
  waiting: ['Waiting on you', 'am'],
  resumable: ['Resumable', 'am'],
  done: ['Done', 'sl'],
}

/** Scrolls itself into view when it appears (a header primary opened it). */
function Reveal({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    ref.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [])
  return <div ref={ref} className="reveal">{children}</div>
}

function Banner({ tone, icon, children, action }: { tone: Tone; icon: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`banner banner--${tone}`} role="status">
      <Icon d={icon} size={18} />
      <div className="banner__text">{children}</div>
      {action && <div className="banner__action">{action}</div>}
    </div>
  )
}

function Attention({ hub }: { hub: HubContext }) {
  const { d, state, actions, setForm, askClearHold } = hub
  const cp = d.checkpoint
  const row = d.row
  if (d.merged_into) {
    return (
      <Banner tone="sl" icon={P.info}>
        This listing was merged into{' '}
        <Link to={`/jobs/${d.merged_into.id}`}><b>{d.merged_into.company} · {d.merged_into.title}</b> →</Link>
      </Banner>
    )
  }
  if (d.open_prompt) {
    return (
      <Banner tone="am" icon={P.chat} action={<button type="button" className="link-btn" onClick={() => actions.openChat()}>Answer →</button>}>
        <b>Waiting on you:</b> {d.open_prompt.question}
      </Banner>
    )
  }
  if (state === 'interrupted' && cp) {
    return (
      <Banner tone="am" icon={P.pause} action={<button type="button" className="link-btn" onClick={actions.resume}>Continue →</button>}>
        <b>Session interrupted</b> at <span className="mono">{cp.step}</span> — resume{' '}
        <span className="mono">{cp.resume_count + 1} of {cp.max_resumes}</span>. Your earlier answers and notes carry over.
      </Banner>
    )
  }
  if (state === 'held') {
    return (
      <Banner tone="am" icon={P.alert} action={<>
        <button type="button" className="link-btn" onClick={() => setForm('submitted')}>It was submitted</button>
        <span aria-hidden="true">·</span>
        <button type="button" className="link-btn link-btn--danger" onClick={askClearHold}>Not submitted — clear hold</button>
      </>}>
        <b>Held</b> — the agent may already have submitted this. Check the employer’s site.
      </Banner>
    )
  }
  if (state === 'failed' && row) {
    return (
      <Banner tone="ro" icon={P.refused} action={row.application_id
        ? <a href={`/api/transcript/${row.application_id}`} target="_blank" rel="noreferrer">Transcript →</a> : undefined}>
        <b>Failed permanently:</b> <span className="mono">{row.failure_reason ?? 'unknown reason'}</span>
      </Banner>
    )
  }
  return null
}

function ScoreCard({ hub }: { hub: HubContext }) {
  const { d } = hub
  const [latest, ...older] = d.assessments
  const [showOlder, setShowOlder] = useState(false)
  return (
    <section className="card">
      <div className="ch">
        <span className="ct">Score</span>
        <span className="hub-note">gate threshold <span className="mono">{d.gate_threshold}</span></span>
      </div>
      {!latest ? (
        <p className="card-body muted-text">Not scored yet.</p>
      ) : latest.stage === 'hard' ? (
        <p className="card-body why-text"><b>Hard filter:</b> {latest.rationale}</p>
      ) : (
        <div className="score-body">
          <div className="score-main">
            <div>
              <span className="mono score-big">{latest.weighted_score != null ? Math.round(latest.weighted_score) : '—'}</span>
              <span className="mono score-of-big">/100</span>
            </div>
            <span className={`b ${VERDICT[latest.verdict][1]}`}>{VERDICT[latest.verdict][0]}</span>
            <p className="why-text">{latest.rationale}</p>
          </div>
          <ScoreBars scores={latest} labelWidth={140} />
        </div>
      )}
      {latest && (
        <div className="score-foot">
          <span className="mono">{latest.model} · prompt {latest.prompt_version} · scored {dateTime(latest.created_at).slice(0, 10)}</span>
          {older.length > 0 && (
            <button type="button" className="link-btn link-btn--muted" aria-expanded={showOlder}
                    onClick={() => setShowOlder((o) => !o)}>
              Previous assessments ({older.length})<Icon d={showOlder ? P.chevronDown : P.chevronRight} />
            </button>
          )}
        </div>
      )}
      {showOlder && (
        <div className="older">
          {older.map((a, i) => (
            <div className="older__row" key={i}>
              <span className={`b ${VERDICT[a.verdict][1]}`}>{a.stage === 'hard' ? 'Hard filter' : VERDICT[a.verdict][0]}</span>
              <span className="mono">{a.weighted_score != null ? `${Math.round(a.weighted_score)}/100` : '—'}</span>
              <span className="mono older__meta">{a.model} · prompt {a.prompt_version} · {dateTime(a.created_at).slice(0, 10)}</span>
              <p className="older__why">{a.rationale}</p>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ResumeCard({ hub }: { hub: HubContext }) {
  const r = hub.d.resume
  return (
    <section className="card">
      <div className="ch">
        <span className="ct">Tailored resume</span>
        {r && (
          <span className="resume-head">
            <span className="mono">{r.version}</span>
            <a href={`/resume/${r.version}`}>Download</a>
          </span>
        )}
      </div>
      {!r ? (
        <p className="card-body muted-text">Not tailored yet — tailoring runs on first Apply.</p>
      ) : (
        <div className="card-body resume-body">
          {r.summary && <p className="why-text">{r.summary}</p>}
          <ul className="bullets">
            {r.bullets.map((b, i) => (
              <li key={i}>
                <span>{b.text}</span>
                {b.fact_ids.length > 0 && (
                  <span className="facts">
                    {b.fact_ids.map((f, j) => {
                      const claim = b.fact_claims[j]
                      return claim == null ? (
                        <span className="fact fact--gone" key={f}><span className="mono">#{f}</span>Unknown fact</span>
                      ) : (
                        <Link className="fact" key={f} to="/facts" title={claim}>
                          <span className="mono">#{f}</span>{claim}
                        </Link>
                      )
                    })}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

const BREAK = /<br\s*\/?>|<\/(?:p|div|li|h[1-6]|tr)>/gi

/** Scraped descriptions carry HTML (<br>, entities). Show them as plain text:
 * <br> and block ends become line breaks and tags drop out. Nothing is ever
 * rendered as HTML, and DOMParser doesn't run scripts. */
function plainText(html: string): string {
  const text = (s: string) =>
    new DOMParser().parseFromString(s.replace(BREAK, '\n'), 'text/html').body.textContent ?? ''
  // Greenhouse sends its markup HTML-escaped, so that takes a second pass.
  const once = text(html)
  return (html.includes('&lt;') ? text(once) : once).replace(/\n\s*\n\s*/g, '\n\n').trim()
}

function Description({ html }: { html: string | null }) {
  const [all, setAll] = useState(false)
  const text = html ? plainText(html) : ''
  return (
    <section className="card">
      <div className="ch"><span className="ct">Job description</span></div>
      <div className="card-body">
        {text ? (
          <>
            <p className={all ? 'jd' : 'jd jd--clamped'}>{text}</p>
            {text.length > 400 && (
              <button type="button" className="link-btn" onClick={() => setAll((a) => !a)}>
                {all ? 'Show less' : 'Show all'}
              </button>
            )}
          </>
        ) : (
          <p className="muted-text">No description was captured for this posting.</p>
        )}
      </div>
    </section>
  )
}

function Attempts({ hub }: { hub: HubContext }) {
  const { d, state, actions, form } = hub
  const n = d.applications.length
  const submitted = d.applications.find((a) => a.status === 'submitted')
  return (
    <section className="card">
      <div className="ch">
        <span className="ct">Applications</span>
        <span className="mono hub-note">{n} attempt{n === 1 ? '' : 's'}</span>
      </div>
      <div className="card-body attempts">
        {n === 0 && <span className="muted-text">No applications yet.</span>}
        {d.applications.map((a) => {
          const [label, tone] = ATTEMPT_BADGE[a.status]
          const when = a.submitted_at ?? a.started_at
          return (
            <div className="attempt" key={a.id}>
              <div className="attempt__row">
                <span className="attempt__status">
                  <span className={`b ${tone}`}>{label}</span>
                  <span className="attempt__note">{a.status === 'draft' ? 'saved for review' : a.failure_reason}</span>
                </span>
                {when && <span className="mono attempt__date">{dateTime(when).slice(0, 10)}</span>}
              </div>
              <div className="attempt__row attempt__meta">
                <span>Resume <span className="mono">{a.resume_version}</span></span>
                {a.has_transcript && (
                  <a href={`/api/transcript/${a.id}`} target="_blank" rel="noreferrer">Transcript</a>
                )}
              </div>
              {a.status === 'submitted' && (
                <div className="outcomes-list">
                  {a.effective_outcome ? (
                    <span className={`b ${OUTCOME_TONE[a.effective_outcome] ?? 'sl'}`}>
                      {d.outcome_labels[a.effective_outcome] ?? a.effective_outcome}
                    </span>
                  ) : (
                    <span className="b sl">Awaiting response</span>
                  )}
                  {a.outcomes.map((o, i) => (
                    <span className="outcome-line" key={i}>
                      {o.type === 'no_response' && o.derived
                        ? 'No response — derived after 30 days'
                        : d.outcome_labels[o.type] ?? o.type}
                      {' · '}<span className="mono">{o.occurred_at.slice(0, 10)}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
        {form === 'outcome' && submitted && (
          <Reveal>
            <OutcomeForm applicationId={submitted.id} outcome={submitted.effective_outcome} actions={actions}
                         manualTypes={d.manual_types} outcomeLabels={d.outcome_labels} today={d.today} />
          </Reveal>
        )}
        {(form === 'submitted' && state === 'held') && (
          <Reveal><AppliedForm jobId={d.job.id} today={d.today} label="It was submitted" actions={actions} /></Reveal>
        )}
        {form === 'applied' && (
          <Reveal><AppliedForm jobId={d.job.id} today={d.today} label="Mark applied" actions={actions} /></Reveal>
        )}
      </div>
    </section>
  )
}

function Session({ hub }: { hub: HubContext }) {
  const cp = hub.d.checkpoint
  if (!cp) return null
  const [label, tone] = SESSION_BADGE[cp.status] ?? [cp.status, 'sl' as Tone]
  let form: string | null = null
  if (cp.form_url) {
    try {
      const u = new URL(cp.form_url)
      form = u.host + u.pathname
    } catch {
      form = cp.form_url
    }
  }
  return (
    <section className="card">
      <div className="ch"><span className="ct">Apply session</span><span className={`b ${tone}`}>{label}</span></div>
      <div className="card-body session">
        <span className="session__k">Mode</span>
        <span>{cp.mode ? <span className="mode-chip mono">{cp.mode}</span> : '—'}</span>
        <span className="session__k">Step</span>
        <span className="mono">{cp.step}</span>
        <span className="session__k">Resumes used</span>
        <span className="mono">{cp.resume_count} of {cp.max_resumes}</span>
        <span className="session__k">Notes pinned</span>
        <span>{cp.notes_count} for the agent</span>
        <span className="session__k">Last update</span>
        <span className="mono">{dateTime(cp.updated_at)}</span>
        {form && (
          <>
            <span className="session__k">Form</span>
            <a className="session__link" href={cp.form_url!} target="_blank" rel="noreferrer" title={cp.form_url!}>{form} ↗</a>
          </>
        )}
      </div>
    </section>
  )
}

function Timeline({ hub }: { hub: HubContext }) {
  const { d } = hub
  const scored = d.assessments.find((a) => a.stage === 'scored')
  const hard = d.assessments.find((a) => a.stage === 'hard')
  const source = SOURCE[d.job.source] ?? d.job.source
  return (
    <section className="card">
      <div className="ch"><span className="ct">Timeline</span></div>
      <div className="timeline">
        {d.events.map((e, i) => {
          const [label, tone] = describe(e)
          const you = e.type.startsWith('human_') || e.type === 'needs_answer_resolved'
          const text = you ? label.replace(/^You /, '') : label
          return (
            <div className="tl" key={i} title={e.payload ?? undefined}>
              <span className={`dot dot--lg dot--${tone}`} />
              <div className="tl__text">
                <span>
                  {you && <span className="b sl tl__you">You</span>}
                  {you ? text.charAt(0).toUpperCase() + text.slice(1) : text}
                </span>
                <span className="mono tl__meta">{e.type} · {stamp(e.occurred_at)}</span>
              </div>
            </div>
          )
        })}
        <div className="tl">
          <span className="dot dot--lg dot--faint" />
          <div className="tl__text">
            <span>
              Discovered on {source}
              {scored?.weighted_score != null ? `, scored ${Math.round(scored.weighted_score)}` : hard ? ', hard-filtered' : ''}
            </span>
            <span className="mono tl__meta">{stamp(d.job.discovered_at)}</span>
          </div>
        </div>
      </div>
    </section>
  )
}

/** The job hub's Details tab (approved Screen 4). */
export function JobDetails() {
  const hub = useOutletContext<HubContext>()
  return (
    <div className="hub-body">
      <Attention hub={hub} />
      <div className="hub-cols">
        <div className="hub-main">
          <div className="o-score"><ScoreCard hub={hub} /></div>
          <div className="o-resume"><ResumeCard hub={hub} /></div>
          <div className="o-desc"><Description html={hub.d.job.description} /></div>
        </div>
        <div className="hub-side">
          <div className="o-apps"><Attempts hub={hub} /></div>
          <div className="o-session"><Session hub={hub} /></div>
          <div className="o-timeline"><Timeline hub={hub} /></div>
        </div>
      </div>
    </div>
  )
}
