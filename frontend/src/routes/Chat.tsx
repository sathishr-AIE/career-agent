import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { errorText, get, post, type ChatMessage, type Conversation, type OpenPrompt } from '../api'
import { Glass } from '../components/Glass'
import { Composer } from '../components/chat/Composer'
import { Drawer, PANEL_KEYS, panelTitle } from '../components/chat/Drawer'
import { MessageList } from '../components/chat/MessageList'
import '../components/ui.css'
import './Chat.css'

const POLL_MS = 3000

/** The chat shell: conversation list, transcript, composer, and a rail that
 * slides the pre-chat pages in from the right. Both lists poll on the same
 * 3s beat the status bar already uses; messages use an exclusive `after`
 * cursor so a poll only ever appends. */
export function Chat() {
  const { id } = useParams()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const [convs, setConvs] = useState<Conversation[]>([])
  const [homeId, setHomeId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [openPrompt, setOpenPrompt] = useState<OpenPrompt | null>(null)
  const [resumable, setResumable] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  const lastId = useRef(0)
  // A clicked Continue waits for the first chat line after its own: the resumed
  // run either takes the job (resumable goes false) or reports why it didn't.
  const resumeAfter = useRef<number | null>(null)
  // Which conversation the pane currently belongs to. A fetch that was
  // already in flight when you switched conversations must not append its
  // messages (or move the cursor) into the new one.
  const shown = useRef<number | null>(null)
  const inFlight = useRef(false)
  const cid = id ? Number(id) : homeId

  const loadConvs = useCallback(() => {
    get<{ conversations: Conversation[]; home_id: number }>('/api/chat/conversations')
      .then((r) => {
        setConvs(r.conversations)
        setHomeId(r.home_id)
      })
      .catch(() => {
        /* transient poll failure -- keep the last known list */
      })
  }, [])

  const loadMessages = useCallback(() => {
    // One request at a time: the interval and send()'s follow-up would
    // otherwise read the same cursor and append the same messages twice.
    if (!cid || inFlight.current) return Promise.resolve()
    inFlight.current = true
    return get<{ messages: ChatMessage[]; open_prompt: OpenPrompt | null; resumable: boolean }>(
      `/api/chat/${cid}/messages?after=${lastId.current}`,
    )
      .then((r) => {
        if (shown.current !== cid) return
        if (r.messages.length) {
          lastId.current = Math.max(lastId.current, r.messages[r.messages.length - 1].id)
          setMessages((m) => {
            const seen = new Set(m.map((x) => x.id))
            return [...m, ...r.messages.filter((x) => !seen.has(x.id))]
          })
        }
        setOpenPrompt(r.open_prompt)
        setResumable(r.resumable)
        const after = resumeAfter.current
        if (!r.resumable || (after !== null && r.messages.some((x) => x.id > after))) {
          resumeAfter.current = null
          setResuming(false)
        }
      })
      .catch(() => {
        /* transient poll failure -- the next tick re-asks from the same cursor */
      })
      .finally(() => {
        inFlight.current = false
      })
  }, [cid])

  useEffect(() => {
    loadConvs()
    const t = setInterval(loadConvs, POLL_MS)
    return () => clearInterval(t)
  }, [loadConvs])

  // Keyed on loadMessages, i.e. on cid: switching conversations rewinds the
  // cursor and empties the pane so the other transcript can't bleed in.
  useEffect(() => {
    shown.current = cid
    inFlight.current = false // the old conversation's request is discarded by `shown`
    lastId.current = 0
    setMessages([])
    setOpenPrompt(null)
    setResumable(false)
    setResuming(false)
    resumeAfter.current = null
    setResumeError(null)
    loadMessages()
    const t = setInterval(loadMessages, POLL_MS)
    return () => clearInterval(t)
  }, [loadMessages])

  const send = (text: string) =>
    cid ? post(`/api/chat/${cid}/messages`, { text }).then(loadMessages) : Promise.resolve()

  // An interrupted session (checkpoint `resumable`): continue it in the same
  // session. A refusal (a live run, a held attempt, ...) is shown under the button.
  const jobId = convs.find((c) => c.id === cid)?.job_id
  const continueRun = () => {
    if (!jobId) return
    setResuming(true)
    setResumeError(null)
    post<{ ok: boolean; message: string; after: number }>(`/api/chat/jobs/${jobId}/resume`)
      .then((r) => {
        resumeAfter.current = r.after
        return loadMessages()
      })
      .catch((e) => {
        setResumeError(errorText(e))
        setResuming(false)
      })
  }

  const panel = params.get('panel')

  return (
    <div className="chat">
      <Glass as="aside" className="chat__list">
        {convs.map((c) => (
          <button
            key={c.id}
            type="button"
            className={c.id === cid ? 'conv active' : 'conv'}
            onClick={() => nav(c.kind === 'home' ? '/' : `/chat/${c.id}`)}
          >
            <div className="conv__title">{c.title}</div>
            <div className="conv__last">{c.last_message ?? ''}</div>
          </button>
        ))}
      </Glass>
      <main className="chat__main">
        <MessageList messages={messages} openPrompt={openPrompt} onAnswered={loadMessages} />
        {resumable && (
          <div className="chat__resume">
            <button type="button" className="btn primary" disabled={resuming || !jobId} onClick={continueRun}>
              Continue where it left off
            </button>
            {resumeError && <div className="qcard__error" role="alert">{resumeError}</div>}
          </div>
        )}
        <Composer onSend={send} disabled={!cid} />
      </main>
      <nav className="chat__rail" aria-label="Panels">
        {PANEL_KEYS.map((p) => (
          <button
            key={p}
            type="button"
            className={panel === p ? 'active' : undefined}
            aria-pressed={panel === p}
            onClick={() => setParams(panel === p ? {} : { panel: p })}
          >
            {panelTitle(p)}
          </button>
        ))}
      </nav>
      {panel && <Drawer panel={panel} onClose={() => setParams({})} />}
    </div>
  )
}
