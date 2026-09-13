import { useEffect, useRef } from 'react'
import type { ChatMessage, OpenPrompt } from '../../api'
import { ConfirmCard } from './ConfirmCard'
import { PromptCard } from './PromptCard'
import './cards.css'

const CONFIRM_SUMMARIES = ['Approved the application', 'Cancelled this application', 'Change: ']

/** Whether a closed prompt message was answered, read off the user summary
 * line actions.answer_prompt posts right after it (ASK: "<question> → ...";
 * CONFIRM: one of CONFIRM_SUMMARIES). ponytail: message payloads carry only
 * {prompt_id, kind}, not the prompt's status -- swap this for a real status
 * field if the backend ever exposes one. */
function wasAnswered(messages: ChatMessage[], index: number): boolean {
  const m = messages[index]
  const prefixes = m.payload?.kind === 'confirm' ? CONFIRM_SUMMARIES : [`${m.content} → `]
  return messages
    .slice(index + 1)
    .some((x) => x.role === 'user' && prefixes.some((p) => x.content.startsWith(p)))
}

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
      {messages.map((m, i) => {
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
            {m.role === 'prompt' && (
              <div className="msg__tag">{wasAnswered(messages, i) ? 'answered' : 'closed'}</div>
            )}
          </div>
        )
      })}
      <div ref={end} />
    </div>
  )
}
