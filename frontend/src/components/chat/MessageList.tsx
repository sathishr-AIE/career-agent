import { useEffect, useRef } from 'react'
import type { ChatMessage, OpenPrompt } from '../../api'

/** The transcript. Roles are the only styling axis: `user` right-aligned,
 * `agent` left, `system` muted and small, `prompt` a question card.
 * `openPrompt`/`onAnswered` are the S2 answer-card seam -- carried now so
 * that slice only swaps the placeholder below for a real form. */
export function MessageList({
  messages,
  openPrompt,
  onAnswered,
}: {
  messages: ChatMessage[]
  openPrompt: OpenPrompt | null
  onAnswered: () => void
}) {
  void openPrompt
  void onAnswered
  const end = useRef<HTMLDivElement>(null)

  // Newest message wins the viewport -- only on append, so reading back
  // through history isn't yanked away by the 3s poll.
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [messages.length])

  return (
    <div className="msgs">
      {messages.map((m) => (
        <div key={m.id} className={`msg msg--${m.role}`}>
          <div className="msg__body">{m.content}</div>
          {m.role === 'prompt' && (
            <div className="msg__pending">Awaiting your answer (card arrives in S2)</div>
          )}
        </div>
      ))}
      <div ref={end} />
    </div>
  )
}
