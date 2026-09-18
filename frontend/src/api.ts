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
  role_fit: number | null
  credibility: number | null
  opportunity: number | null
  application_quality: number | null
  eligibility_soft: number | null
  terminal_status: string | null
  has_draft: number
  resume_version: string | null
  application_id: number | null
  failure_reason: string | null
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
    resumable: number
  }
  recent_events: { type: string; payload: string | null; occurred_at: string }[]
  open_prompt: { id: number; kind: string; question: string; needs_answer: boolean } | null
  /** Every open job card (OC1), for "N cards waiting". */
  open_prompt_count: number
  conversation_id: number | null
  submission_implemented: boolean
}

export interface PipelineState {
  status: 'idle' | 'running' | 'paused' | 'stopped' | 'error'
  last_error: string | null
  /** SQLite's naive-UTC datetime('now'). */
  started_at: string | null
  stage: string | null
  found: number
  duplicates: number
  passed: number
  scored: number
  shortlisted: number
}

/** GET /api/pipeline/status (context.pipeline_status_context). */
export interface PipelineStatus {
  pipeline_state: PipelineState
  feed: { payload: string; occurred_at: string }[]
  stages: [string, string][]
  max_score: number
}

export interface ActionResult {
  ok: boolean
  message: string
  changes?: string[]
}

// -- chat (career_agent/chat.py + web/api_chat.py) --

export interface Conversation {
  id: number
  kind: 'home' | 'job'
  job_id: number | null
  title: string
  updated_at: string
  last_message: string | null
}

export interface ChatMessage {
  id: number
  role: 'user' | 'agent' | 'system' | 'prompt'
  content: string
  payload: Record<string, unknown> | null
  created_at: string
  /** Only on `prompt` messages: the agent_prompt's current status. */
  prompt_status?: 'open' | 'answered' | 'expired'
}

export interface OpenPrompt {
  id: number
  kind: string
  payload: Record<string, unknown>
  created_at: string
}

/** An ASK card's payload (apply/agent.py parse_ask). The account kinds also
 * carry domain/email/terms_summary. */
export interface AskPayload {
  id: string
  kind: 'choice' | 'text' | 'approve' | 'approve_account' | 'need_password'
  question: string
  options: string[]
  why: string
  memory_key: string | null
  default: string | null
  sensitive: boolean
  domain?: string
  email?: string
  login_url?: string
  url?: string
  /** approve_account: the browser's real page urls, added by the backend. */
  page_urls?: string[]
  terms_summary?: string
}

/** A CONFIRM card's payload (apply/agent.py parse_confirm). */
export interface ConfirmPayload {
  fields: { label: string; value: string }[]
  files: string[]
  account_actions: string[]
  memory_used: string[]
  notes: string
}

/** Body is the bare dict -- api_answer_prompt reads the whole JSON body:
 * {answer, remember} for ASK, {decision, changes?} for CONFIRM. */
export const answerPrompt = (promptId: number, body: Record<string, unknown>) =>
  post<ActionResult>(`/api/chat/prompts/${promptId}/answer`, body)

/** The backend's refusal text, for inline display. `detail` is only used
 * when it's a string (FastAPI's own 422s make it an array). */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const b = e.body as { detail?: unknown; message?: unknown } | undefined
    if (typeof b?.detail === 'string') return b.detail
    if (typeof b?.message === 'string') return b.message
  }
  return e instanceof Error ? e.message : String(e)
}
