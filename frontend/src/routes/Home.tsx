import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, post, put, type Conversation } from '../api'
import { Icon } from '../components/Icon'
import { P } from '../icons'
import { Composer } from '../components/chat/Composer'
import { ModelPicker } from '../components/chat/ModelPicker'
import { Transcript, TranscriptSkeleton } from '../components/chat/Transcript'
import { useTranscript } from '../components/chat/useTranscript'
import { TopBarActions, useShell } from '../components/shell'
import { usePoll } from '../components/usePoll'
import { ago } from '../time'
import '../components/chat/chat.css'
import './Home.css'

interface Convs {
  conversations: Conversation[]
  home_id: number
}

interface Models {
  scoring_model: string
  scoring_models: string[]
  model_labels: Record<string, string>
}

/** The empty transcript's openers. They send the literal text, so the
 * backend's own intent router decides what each one does. */
const STARTERS = ['Find new jobs', "What's in my queue?", 'Status', 'Pause applying']

const SECRET_NOTE = 'Secrets are never typed into chat — saved logins are filled by the backend; manage them in '

function Rail({ convs, waitingId, onPick }: {
  convs: Conversation[]
  waitingId: number | null
  onPick?: () => void
}) {
  if (!convs.length) return <div className="empty">No job chats yet</div>
  return (
    <div className="card">
      {convs.map((c) => (
        <Link key={c.id} className="conv-row" to={`/jobs/${c.job_id}/chat`} onClick={onPick}>
          <span className="conv-row__top">
            <span className="conv-row__title">
              {c.id === waitingId && <span className="dot dot--am" />}
              {c.title}
            </span>
            <span className="mono conv-row__age">{ago(c.updated_at)}</span>
          </span>
          {c.last_message && <span className="conv-row__last">{c.last_message}</span>}
        </Link>
      ))}
    </div>
  )
}

export function Home() {
  const { run, toast } = useShell()
  const { data: convs } = usePoll<Convs>('/api/chat/conversations')
  const { data: models } = usePoll<Models>('/api/settings/models', 0)
  const cid = convs?.home_id ?? null
  const { messages, openPrompt, offline, reload } = useTranscript(cid)

  const [picked, setPicked] = useState<string | null>(null)
  const [pending, setPending] = useState<{ text: string; at: number } | null>(null)
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState<{ text: string; secret: boolean } | null>(null)
  const [sheet, setSheet] = useState(false)

  const model = picked ?? models?.scoring_model ?? null
  const jobs = (convs?.conversations ?? []).filter((c) => c.kind === 'job')
  const waitingId = run?.open_prompt ? run.conversation_id : null

  useEffect(() => {
    if (!sheet) return
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setSheet(false)
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [sheet])

  const send = (text: string) => {
    if (cid === null) return Promise.reject(new Error('no conversation'))
    setPending({ text, at: messages?.length ?? 0 })
    setSending(true)
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
      .finally(() => setSending(false))
  }

  const pickModel = (id: string) => {
    put<{ scoring_model: string }>('/api/settings/models', { scoring_model: id })
      .then(() => {
        setPicked(id)
        toast({ text: 'Scoring model saved' })
      })
      .catch(() => toast({ text: 'Could not save the scoring model', tone: 'error' }))
  }

  // The optimistic bubble stands in only until the poll returns the real
  // row (the backend writes it before the classifier runs).
  const ghost = pending && messages && pending.at === messages.length ? pending.text : null

  return (
    <div className="home">
      <TopBarActions>
        <button className="btn home__convs" onClick={() => setSheet(true)}>Conversations</button>
      </TopBarActions>

      <div className="home__col">
        <div className="home__scroll">
          {offline && (
            <div className="notice" role="status">
              <Icon d={P.info} size={16} />
              <span>Can't reach the server — retrying</span>
            </div>
          )}
          {messages === null ? (
            <TranscriptSkeleton />
          ) : messages.length === 0 ? (
            <div className="home__start">
              <p className="home__welcome">Ask for what you want — finding jobs and applying need your go-ahead first.</p>
              <div className="home__chips">
                {STARTERS.map((s) => (
                  <button key={s} className="chip home__chip" onClick={() => void send(s)}>{s}</button>
                ))}
              </div>
            </div>
          ) : (
            <Transcript messages={messages} openPrompt={openPrompt} onAnswered={reload}
                        pending={ghost} thinking={sending} />
          )}
        </div>

        {refusal && (
          <div className="alert home__refusal" role="alert">
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
          invalid={Boolean(refusal?.secret)}
          toolbar={
            <ModelPicker label="Scoring model" value={model}
                         options={models?.scoring_models ?? []}
                         optionLabel={(id) => models?.model_labels[id] ?? id}
                         onPick={pickModel} busy={!models} />
          }
        />
      </div>

      <aside className="home__rail">
        <span className="lbl">Conversations</span>
        <Rail convs={jobs} waitingId={waitingId} />
      </aside>

      {sheet && (
        <>
          <div className="scrim home__scrim" onClick={() => setSheet(false)} />
          <aside className="home__sheet" role="dialog" aria-label="Conversations">
            <div className="home__sheet-head">
              <span className="lbl">Conversations</span>
              <button className="ib" onClick={() => setSheet(false)} aria-label="Close">
                <Icon d={P.x} size={16} />
              </button>
            </div>
            <Rail convs={jobs} waitingId={waitingId} onPick={() => setSheet(false)} />
          </aside>
        </>
      )}
    </div>
  )
}
