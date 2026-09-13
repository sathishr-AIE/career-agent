import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, type RunStatusContext } from '../api'
import { Glass } from './Glass'
import './AgentStatusBar.css'

const POLL_MS = 3000

/** Pinned to the bottom of every route so you never navigate to find out
 * whether the machine is running -- see the design plan's "docket, not a
 * dashboard" framing. Polls the one run/status endpoint that both the
 * Jinja fragment and this component read from. */
export function AgentStatusBar() {
  const [status, setStatus] = useState<RunStatusContext | null>(null)

  useEffect(() => {
    let cancelled = false
    const tick = () => {
      get<RunStatusContext>('/api/run/status')
        .then((s) => {
          if (!cancelled) setStatus(s)
        })
        .catch(() => {
          /* transient poll failure -- keep showing the last known state */
        })
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (!status) return null
  const { run_state, current_job, open_prompt, conversation_id, submission_implemented } = status

  return (
    <Glass as="footer" className="status-bar">
      <span className={`status-bar__dot status-bar__dot--${run_state.status}`} />
      <span className="status-bar__label">
        {run_state.status}
        {run_state.mode ? ` · ${run_state.mode}` : ''}
      </span>
      {current_job && (
        <span className="status-bar__job">
          {current_job.company} — {current_job.title}
        </span>
      )}
      <span className="status-bar__spacer" />
      {open_prompt && conversation_id && (
        <span className="status-bar__answer">
          {open_prompt.question}
          <Link to={`/chat/${conversation_id}`}>Answer in chat →</Link>
        </span>
      )}
      {!submission_implemented && (
        <span className="status-bar__killswitch" title="Real submission is disabled; every apply drafts only.">
          Submission off
        </span>
      )}
    </Glass>
  )
}
