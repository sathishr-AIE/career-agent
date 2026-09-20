import { useState } from 'react'
import { DIMENSIONS, OUTCOME_TONE, type DimKey, type State } from '../jobState'
import type { JobActions } from './useJobActions'
import './JobParts.css'

/** A job's status badge: the same label on Applications and the job hub. */
export function StatusBadge({ state, reason, waiting, outcome, outcomeLabels }: {
  state: State
  reason?: string | null
  /** In progress with an open card: amber "Waiting on you". */
  waiting?: boolean
  /** A submitted application's effective outcome. */
  outcome?: string | null
  outcomeLabels: Record<string, string>
}) {
  const why = reason && <span className="reason mono">{reason}</span>
  switch (state) {
    case 'in_progress':
      return waiting ? (
        <span className="b am"><span className="dot dot--am" />Waiting on you</span>
      ) : (
        <span className="b sk"><span className="dot dot--sk" />In progress</span>
      )
    case 'submitted':
      return outcome ? (
        <span className={`b ${OUTCOME_TONE[outcome] ?? 'sl'}`}>{outcomeLabels[outcome] ?? outcome}</span>
      ) : (
        <span className="b sl">Awaiting response</span>
      )
    case 'held':
      return <span className="status-cell"><span className="b am">Held</span>{why}</span>
    case 'failed':
      return <span className="status-cell"><span className="b ro">Failed</span>{why}</span>
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

/** The five gate dimensions as labelled slate bars (0–100). */
export function ScoreBars({ scores, labelWidth = 136 }: {
  scores: Partial<Record<DimKey, number | null>>
  labelWidth?: number
}) {
  const dims = DIMENSIONS.filter(([k]) => scores[k] != null)
  if (dims.length === 0) return null
  return (
    <div className="dims" style={{ gridTemplateColumns: `${labelWidth}px minmax(0, 1fr) 28px` }}>
      {dims.map(([k, label]) => {
        const v = Math.round(scores[k] as number)
        return (
          <div className="dim" key={k}>
            <span>{label}</span>
            <div className="dim__bar"><div style={{ width: `${v}%` }} /></div>
            <span className="mono">{v}</span>
          </div>
        )
      })}
    </div>
  )
}

/** Record a callback outcome on a submitted application. */
export function OutcomeForm({ applicationId, outcome, manualTypes, outcomeLabels, today, actions }: {
  applicationId: number
  outcome: string | null
  manualTypes: string[]
  outcomeLabels: Record<string, string>
  today: string
  actions: JobActions
}) {
  const [type, setType] = useState(manualTypes[0] ?? '')
  const [date, setDate] = useState(today)
  const [notes, setNotes] = useState('')
  return (
    <form className="row-form" onSubmit={(e) => {
      e.preventDefault()
      actions.act(`/api/outcome/${applicationId}`, { type, occurred_at: date, notes }, 'Outcome recorded')
    }}>
      <span className="lbl">Record outcome</span>
      <span className="row-form__current">
        Current: {outcome
          ? <b className={`tone-${OUTCOME_TONE[outcome] ?? 'sl'}`}>{outcomeLabels[outcome] ?? outcome}</b>
          : 'Awaiting response'}
      </span>
      <div className="row-form__pair">
        <select className="field" value={type} disabled={actions.busy} aria-label="Outcome"
                onChange={(e) => setType(e.target.value)}>
          {manualTypes.map((t) => <option key={t} value={t}>{outcomeLabels[t] ?? t}</option>)}
        </select>
        <input className="field mono" type="date" value={date} disabled={actions.busy} aria-label="Date"
               onChange={(e) => setDate(e.target.value)} />
      </div>
      <input className="field" type="text" placeholder="Notes (optional)" value={notes} disabled={actions.busy}
             aria-label="Notes" onChange={(e) => setNotes(e.target.value)} />
      <div><button type="submit" className="btn" disabled={actions.busy}>Record</button></div>
    </form>
  )
}

/** "Mark applied" / "It was submitted": record a submission made outside the agent. */
export function AppliedForm({ jobId, today, label, actions }: {
  jobId: number
  today: string
  label: 'Mark applied' | 'It was submitted'
  actions: JobActions
}) {
  const [date, setDate] = useState(today)
  return (
    <form className="row-form" onSubmit={(e) => {
      e.preventDefault()
      actions.act(`/api/applied/${jobId}`, { when: date }, 'Marked applied')
    }}>
      <span className="lbl">{label}</span>
      <span className="row-form__current">
        {label === 'It was submitted'
          ? 'Check the employer’s site first. This records it as submitted on this date.'
          : 'You applied on the site yourself.'}
      </span>
      <div className="row-form__pair">
        <input className="field mono" type="date" value={date} disabled={actions.busy} aria-label="Date"
               onChange={(e) => setDate(e.target.value)} />
        <button type="submit" className="btn" disabled={actions.busy}>{label}</button>
      </div>
    </form>
  )
}
