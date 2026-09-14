import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import type { ChatMessage, OpenPrompt } from '../../api'
import { ConfirmCard } from './ConfirmCard'
import { PromptCard } from './PromptCard'
import './cards.css'

/** The transcript. Roles are the only styling axis: `user` right-aligned,
 * `agent` left, `system` muted and small, `prompt` a question -- an answer
 * card while it's the open prompt, plain text with a tag once closed. */
export function MessageList({
  messages,
  openPrompt,
  onAnswered,
}: {
  messages: ChatMessage[]
  openPrompt: OpenPrompt | null
  onAnswered: () => void
}) {
  const end = useRef<HTMLDivElement>(null)

  // Newest message wins the viewport -- only on append, so reading back
  // through history isn't yanked away by the 3s poll.
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [messages.length])

  return (
    <div className="msgs">
      {messages.map((m) => {
        if (m.role === 'prompt' && openPrompt && m.payload?.prompt_id === openPrompt.id) {
          const Card = openPrompt.kind === 'confirm' ? ConfirmCard : PromptCard
          return (
            <div key={m.id} className="msg msg--prompt">
              {/* keyed by prompt id so a new prompt never inherits the last card's state */}
              <Card key={openPrompt.id} prompt={openPrompt} onAnswered={onAnswered} />
            </div>
          )
        }
        return (
          <div key={m.id} className={`msg msg--${m.role}`}>
            <div className="msg__body">{m.content}</div>
            {/* Home's "Started — follow along" line carries the job's conversation */}
            {typeof m.payload?.conversation_id === 'number' && (
              <Link className="btn" to={`/chat/${m.payload.conversation_id}`}>
                Open the job's chat
              </Link>
            )}
            {m.role === 'prompt' && (
              <div className="msg__tag">{m.prompt_status === 'answered' ? 'answered' : 'closed'}</div>
            )}
          </div>
        )
      })}
      <div ref={end} />
    </div>
  )
}
