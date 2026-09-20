import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useOutletContext } from 'react-router-dom'
import { ApiError, get, post, put, type Conversation, type OpenPrompt } from '../api'
import { Icon } from '../components/Icon'
import { P } from '../icons'
import { AskCard } from '../components/chat/AskCard'
import { Composer } from '../components/chat/Composer'
import { ConfirmCard } from '../components/chat/ConfirmCard'
import { ModelPicker } from '../components/chat/ModelPicker'
import { Transcript, TranscriptSkeleton } from '../components/chat/Transcript'
import { useTranscript } from '../components/chat/useTranscript'
import { useShell } from '../components/shell'
import { usePoll } from '../components/usePoll'
import type { HubContext } from './Job'
import '../components/chat/chat.css'
import './JobChat.css'

interface Models {
  apply_model: string
  apply_models: string[]
  model_labels: Record<string, string>
}

const SECRET_NOTE = 'Secrets are never typed into chat — saved logins are filled by the backend; manage them in '
const LOCKED_HINT = 'A running session keeps its model; your choice applies to the next Apply or Continue'

/** The old /chat/:id, kept for links and bookmarks: a conversation knows its
 * job, and the job's chat now lives in its hub. */
export function ChatRedirect() {
  const { id } = useLocation().pathname.match(/\/chat\/(?<id>\d+)/)?.groups ?? {}
  const { data } = usePoll<{ conversations: Conversation[]; home_id: number }>(
    '/api/chat/conversations', 0)
  if (!data) return <div className="jchat__wait"><TranscriptSkeleton /></div>
  const conv = data.conversations.find((c) => String(c.id) === id)
  return <Navigate replace to={conv?.job_id ? `/jobs/${conv.job_id}/chat` : '/'} />
}

export function JobChat() {
  const hub = useOutletContext<HubContext>()
  const { d, actions } = hub
  const { toast, run } = useShell()
  const nav = useLocation()

  // JD1 never creates a conversation (it is a plain lookup), so a job that
  // has never been applied to has none until the tab is opened.
  const [made, setMade] = useState<number | null>(null)
  const cid = d.conversation_id ?? made
  const { messages, openPrompt, resumable, offline, reload } = useTranscript(cid)
  const { data: models } = usePoll<Models>('/api/settings/models', 0)

  const [picked, setPicked] = useState<string | null>(null)
  const [pending, setPending] = useState<{ text: string; at: number } | null>(null)
  const [refusal, setRefusal] = useState<{ text: string; secret: boolean } | null>(null)
  const [after, setAfter] = useState<number | null>(
    (nav.state as { after?: number } | null)?.after ?? null)
  const [resumeError, setResumeError] = useState<string | null>(null)

  useEffect(() => {
    if (d.conversation_id !== null || made !== null) return
    let live = true
    get<{ id: number }>(`/api/chat/jobs/${d.job.id}/conversation`)
      .then((c) => live && setMade(c.id))
      .catch(() => {})
    return () => { live = false }
  }, [d.conversation_id, d.job.id, made])

  const live = d.checkpoint?.status === 'running' || d.checkpoint?.status === 'waiting'
  const model = live ? d.checkpoint?.model ?? null : picked ?? models?.apply_model ?? null

  // Continue is busy until the resumed run speaks: either it takes the job
  // (resumable goes false) or it says why it didn't (a newer line lands).
  const continuing = after !== null && resumable && !messages?.some((m) => m.id > after)

  const send = (text: string) => {
    if (cid === null) return Promise.reject(new Error('no conversation'))
    setPending({ text, at: messages?.length ?? 0 })
    setRefusal(null)
    return post(`/api/chat/${cid}/messages`, { text })
      .then(() => reload())
      .catch((e: unknown) => {
        setPending(null)
        setRefusal({
          text: e instanceof ApiError ? e.message : 'Could not reach the server',
          secret: e instanceof ApiError && e.status === 422,
        })
        throw e                       // the composer keeps the text
      })
  }

  const pickModel = (id: string) => {
    put<{ apply_model: string }>('/api/settings/models', { apply_model: id })
      .then(() => {
        setPicked(id)
        toast({ text: 'Apply model saved' })
      })
      .catch(() => toast({ text: 'Could not save the apply model', tone: 'error' }))
  }

  const continueRun = () => {
    setResumeError(null)
    post<{ after: number }>(`/api/chat/jobs/${d.job.id}/resume`)
      .then((r) => {
        setAfter(r.after)
        reload()
      })
      .catch((e: unknown) => setResumeError(e instanceof ApiError ? e.message : String(e)))
  }

  const ghost = pending && messages && pending.at === messages.length ? pending.text : null

  const card = (p: OpenPrompt) =>
    p.kind === 'confirm'
      ? <ConfirmCard key={p.id} prompt={p} onAnswered={reload} mode={d.checkpoint?.mode}
                     canSubmit={run?.submission_implemented ?? false} />
      : <AskCard key={p.id} prompt={p} onAnswered={reload} />

  return (
    <div className="jchat">
      <div className="jchat__col">
        <div className="jchat__scroll">
          {offline && (
            <div className="notice" role="status">
              <Icon d={P.info} size={16} />
              <span>Can't reach the server — retrying</span>
            </div>
          )}
          {messages === null ? (
            <TranscriptSkeleton />
          ) : messages.length === 0 ? (
            <p className="jchat__empty">
              Nothing here yet — Apply starts the agent and its questions appear here.
            </p>
          ) : (
            <Transcript messages={messages} openPrompt={openPrompt} onAnswered={reload}
                        pending={ghost} card={card} notes />
          )}
        </div>

        <div className="jchat__foot">
          {resumable && (
            <div className="jchat__resume" role="status">
              <Icon d={P.pause} size={16} />
              <span>This session was interrupted — continue where it left off.</span>
              <button className="btn pri" disabled={continuing || actions.busy}
                      onClick={continueRun}>
                {continuing ? 'Continuing…' : 'Continue'}
              </button>
            </div>
          )}
          {resumeError && (
            <div className="alert" role="alert">
              <Icon d={P.refused} size={14} />
              <span>{resumeError}</span>
            </div>
          )}
          {refusal && (
            <div className="alert" role="alert">
              <Icon d={P.refused} size={14} />
              <span>
                <b>Not sent:</b>{' '}
                {refusal.secret ? <>{SECRET_NOTE}<Link to="/logins">Logins</Link>.</> : refusal.text}
              </span>
            </div>
          )}
          <Composer
            onSend={send}
            disabled={cid === null}
            placeholder="Add a note for the agent"
            invalid={Boolean(refusal?.secret)}
            helper="Notes guide this application. They never approve a submission."
            toolbar={
              <ModelPicker label={live ? 'Running on' : 'Apply agent'} value={model}
                           options={models?.apply_models ?? []}
                           optionLabel={(id) => models?.model_labels[id] ?? id}
                           onPick={pickModel} busy={!models}
                           lockedText={live ? LOCKED_HINT : undefined} />
            }
          />
        </div>
      </div>
    </div>
  )
}
