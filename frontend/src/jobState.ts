import type { Job, Verdict } from './api'
import type { Tone } from './events'

/** A job's status, shared by Applications and the job hub so both screens
 * always show the same label (the spec's row-state table). */
export type State =
  | 'in_progress' | 'submitted' | 'held' | 'dismissed' | 'failed' | 'interrupted' | 'drafted' | 'queued'

/** First match wins. `row` is a LIST_SQL row (Applications, or JD1's `row`). */
export function stateOf(j: Job): State {
  if (j.terminal_status === 'in_flight') return 'in_progress'
  if (j.terminal_status === 'submitted') return 'submitted'
  if (j.terminal_status === 'held_unknown') return 'held'
  if (j.dismissed_at) return 'dismissed'
  if (j.terminal_status === 'failed_permanent') return 'failed'
  if (j.resumable) return 'interrupted'
  if (j.has_draft) return 'drafted'
  return 'queued'
}

export const VERDICT: Record<Verdict, [string, Tone]> = {
  submit: ['Submit', 'em'],
  hold: ['Hold', 'am'],
  skip: ['Skip', 'ro'],
}
export const SOURCE: Record<string, string> = { linkedin: 'LinkedIn', naukri: 'Naukri', ats: 'Greenhouse' }
export const OUTCOME_TONE: Record<string, Tone> = { screen: 'em', interview: 'em', offer: 'em', rejected: 'ro' }

export type DimKey = 'role_fit' | 'credibility' | 'opportunity' | 'application_quality' | 'eligibility_soft'
export const DIMENSIONS: [DimKey, string][] = [
  ['role_fit', 'Role fit'],
  ['credibility', 'Credibility'],
  ['opportunity', 'Opportunity'],
  ['application_quality', 'Application quality'],
  ['eligibility_soft', 'Eligibility'],
]

/** A gate skip is applied through the override route. */
export const applyPath = (j: { id: number; verdict: Verdict | null }) =>
  j.verdict === 'skip' ? `/api/override/${j.id}` : `/api/apply/${j.id}`
