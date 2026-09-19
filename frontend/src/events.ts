import { P } from './icons'

/** A status colour: emerald, amber, rose, sky, slate (the design's only tones). */
export type Tone = 'em' | 'am' | 'ro' | 'sk' | 'sl'

// Every `event.type` the backend writes, as a feed label, tone and icon.
const EVENTS: Record<string, [label: string, tone: Tone, icon: string]> = {
  submitted: ['Submitted', 'em', P.check],
  failed: ['Attempt failed', 'ro', P.x],
  failed_permanent: ['Failed permanently', 'ro', P.x],
  held_unknown: ['Held — may have been submitted', 'am', P.alert],
  captcha_held: ['Stopped at a CAPTCHA', 'am', P.alert],
  submitted_without_decision: ['Submitted without your go-ahead', 'ro', P.alert],
  draft_without_decision: ['Draft saved without your go-ahead', 'am', P.file],
  resumable: ['Session resumable', 'sl', P.pause],
  needs_answer: ['Asked you a question', 'am', P.chat],
  needs_answer_resolved: ['You answered', 'sl', P.user],
  human_applied: ['You started applying', 'sl', P.user],
  human_override: ['You tracked a skipped job', 'sl', P.user],
  human_dismissed: ['You dismissed a job', 'sl', P.user],
  human_marked_applied: ['You marked it applied', 'sl', P.user],
  human_restored: ['You put a job back in the queue', 'sl', P.user],
  job_skipped: ['Skipped', 'sl', P.skip],
  hold_cleared: ['Hold cleared — not submitted', 'sl', P.check],
  orphan_in_flight_dropped: ['Cleared an interrupted attempt', 'sl', P.x],
  run_started: ['Apply run started', 'sk', P.play],
  run_paused: ['Apply run paused', 'sl', P.pause],
  run_resumed: ['Apply run resumed', 'sl', P.play],
  run_stopped: ['Apply run stopped', 'sl', P.pause],
  run_completed: ['Apply run finished', 'sl', P.check],
  run_autopaused: ['Apply run paused itself', 'am', P.pause],
  run_error: ['Apply run error', 'ro', P.alert],
}

export function describe(e: { type: string; payload: string | null }): [string, Tone, string] {
  if (e.payload === 'resume_limit' && (e.type === 'failed' || e.type === 'failed_permanent')) {
    return ['Resume limit reached', 'ro', P.x]
  }
  return EVENTS[e.type] ?? [e.type.replace(/_/g, ' '), 'sl', P.file]
}
