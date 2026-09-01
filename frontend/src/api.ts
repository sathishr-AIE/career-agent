// One tiny fetch wrapper for the whole app -- four pages don't need
// react-query or axios. Throws ApiError on any non-2xx so callers can
// catch() once and show the message.

export class ApiError extends Error {
  status: number
  /** The parsed JSON error body, when there was one -- e.g.
   * {ok:false, errors:{field: message}} from PUT /api/settings, or
   * {ok:false, message} from an action route. Callers that need structured
   * errors (Settings' per-field messages) read this instead of `.message`. */
  body: unknown
  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  const isJson = res.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await res.json() : undefined
  if (!res.ok) {
    throw new ApiError(res.status, body?.message ?? `Request failed (${res.status})`, body)
  }
  return body as T
}

export const get = <T>(path: string) => request<T>(path)

export const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body) })

export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(path, { method: 'POST', body: form })
  const body = await res.json()
  if (!res.ok) throw new ApiError(res.status, body?.message ?? `Upload failed (${res.status})`, body)
  return body as T
}

// -- shapes the backend actually returns (career_agent/web/context.py) --

export type Verdict = 'submit' | 'hold' | 'skip'

export interface Job {
  id: number
  company: string
  title: string
  location: string | null
  source: string
  url: string
  verdict: Verdict | null
  rationale: string | null
  stage: string | null
  score: number | null
  terminal_status: string | null
  has_draft: number
  resume_version: string | null
}

export interface RunState {
  kind: string
  status: 'idle' | 'running' | 'paused' | 'stopped' | 'error'
  mode: 'auto' | 'manual' | null
  current_job_id: number | null
  last_error: string | null
  started_at: string | null
}

export interface RunStatusContext {
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
  submission_implemented: boolean
}

export interface ActionResult {
  ok: boolean
  message: string
  changes?: string[]
}
