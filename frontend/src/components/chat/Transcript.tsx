import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Icon, Spinner } from '../Icon'
import { P } from '../../icons'
import { clock, dayLabel } from '../../time'
import { ApproveCard } from './ApproveCard'
import type { ChatMessage, OpenPrompt } from '../../api'

/** Answering a card posts a `user` echo tagged with its decision
 * (actions._answer_home_prompt). It is the card's badge, not a bubble. */
interface Echo { prompt_id: number; decision: string }

function echoOf(m: ChatMessage): Echo | null {
  const p = m.payload as Partial<Echo> | null
  return m.role === 'user' && typeof p?.prompt_id === 'number' && typeof p.decision === 'string'
    ? { prompt_id: p.prompt_id, decision: p.decision }
    : null
}

const DECIDED: Record<string, [string, string]> = {
  approve: ['Approved', 'b em'],
  reject: ['Not now', 'b sl'],
}

function Avatar() {
  return (
    <div className="avatar" aria-hidden="true"><Icon d={P.bolt} size={14} /></div>
  )
}

/** A closed card: its question and what was decided. It does not expand --
 * a closed prompt's payload never comes back with the messages, and the row
 * already says everything Home's cards hold. */
function ClosedCard({ m, echo }: { m: ChatMessage; echo: Echo | undefined }) {
  const kind = (m.payload as { kind?: string } | null)?.kind
  const [text, tone] = echo
    ? DECIDED[echo.decision] ?? ['Answered', 'b sl']
    : m.prompt_status === 'answered' ? ['Answered', 'b em'] : ['Expired', 'b sl']
  return (
    <div className="ccard--closed card">
      <Icon d={P.chevronRight} size={14} />
      <span className="ccard__kind">{kind === 'approve' ? 'Go-ahead' : 'Agent asked'}</span>
      <span className="ccard__closedq">{m.content}</span>
      <span className={tone}>{text}</span>
    </div>
  )
}

function AgentMessage({ m }: { m: ChatMessage }) {
  const cid = (m.payload as { conversation_id?: number } | null)?.conversation_id
  return (
    <div className="agent">
      <Avatar />
      <div className="agent__col">
        <p className="agenttext">{m.content}</p>
        {typeof cid === 'number' && (
          // Interim until Screen 6 moves the job chat into the job hub.
          <Link className="btn agent__open" to={`/chat/${cid}`}>
            Open job chat <Icon d={P.arrowRight} size={14} />
          </Link>
        )}
      </div>
    </div>
  )
}

/** The message list for one conversation. `pending` is the message the
 * composer has sent but the poll hasn't returned yet; `thinking` says the
 * agent is still working (a Home message waits on the intent classifier,
 * which can take seconds). */
export function Transcript({ messages, openPrompt, onAnswered, pending, thinking = false }: {
  messages: ChatMessage[]
  openPrompt: OpenPrompt | null
  onAnswered: () => void
  pending?: string | null
  thinking?: boolean
}) {
  const end = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)
  const [seenId, setSeenId] = useState(0)
  const lastId = messages.length ? messages[messages.length - 1].id : 0

  // The shell's .app__scroll is the scroller, so watch a sentinel rather
  // than measure a container this component doesn't own. Re-observed per
  // message, which is what lets the callback bank the id it has seen.
  useEffect(() => {
    const mark = end.current
    if (!mark) return
    const obs = new IntersectionObserver(([e]) => {
      setAtBottom(e.isIntersecting)
      if (e.isIntersecting) setSeenId(lastId)
    }, { rootMargin: '0px 0px 80px 0px' })
    obs.observe(mark)
    return () => obs.disconnect()
  }, [lastId])

  useEffect(() => {
    if (atBottom) end.current?.scrollIntoView({ block: 'end' })
  }, [atBottom, lastId, pending, thinking])

  const echoes = new Map<number, Echo>()
  for (const m of messages) {
    const e = echoOf(m)
    if (e) echoes.set(e.prompt_id, e)
  }

  // A divider before the first message of each local day.
  const dividers = messages.map((m, i) => {
    const label = dayLabel(m.created_at)
    return i === 0 || label !== dayLabel(messages[i - 1].created_at) ? label : null
  })

  return (
    <div className="tscript" aria-live="polite">
      {messages.map((m, i) => {
        return (
          <div key={m.id} className="tscript__row">
            {dividers[i] && (
              <div className="sys sys--day">
                <span className="rule" /><span className="mono">{dividers[i]}</span><span className="rule" />
              </div>
            )}
            {m.role === 'user' && !echoOf(m) && <div className="user">{m.content}</div>}
            {m.role === 'agent' && <AgentMessage m={m} />}
            {m.role === 'system' && (
              <div className="sys">
                <span className="rule" /><span>{m.content}</span>
                <span className="mono">{clock(m.created_at)}</span><span className="rule" />
              </div>
            )}
            {m.role === 'prompt' && (
              openPrompt && (m.payload as { prompt_id?: number } | null)?.prompt_id === openPrompt.id
                ? <div className="agent"><Avatar />
                    <ApproveCard key={openPrompt.id} prompt={openPrompt} onAnswered={onAnswered} />
                  </div>
                : <ClosedCard m={m} echo={echoes.get((m.payload as { prompt_id?: number } | null)?.prompt_id ?? -1)} />
            )}
          </div>
        )
      })}
      {pending && <div className="user user--pending">{pending}</div>}
      {thinking && (
        <div className="agent"><Avatar />
          <p className="agenttext agenttext--faint"><Spinner /> Working on it…</p>
        </div>
      )}
      <div ref={end} className="tscript__end" />
      {!atBottom && lastId > seenId && (
        <button className="btn sm newmsgs"
                onClick={() => end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })}>
          New messages <Icon d={P.down} size={14} />
        </button>
      )}
    </div>
  )
}

/** Loading: a few message-shaped bars. */
export function TranscriptSkeleton() {
  return (
    <div className="tscript">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className={i % 2 ? 'skel skel--agent' : 'skel skel--user'} />
      ))}
    </div>
  )
}