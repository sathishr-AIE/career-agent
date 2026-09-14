import { useState } from 'react'

/** Enter sends, Shift+Enter newlines. Clears only once the POST resolved,
 * so a failed send leaves the text where the user can retry it. */
export function Composer({
  onSend,
  disabled,
  placeholder = 'Message the agent',
}: {
  onSend: (text: string) => Promise<unknown>
  disabled?: boolean
  placeholder?: string
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = () => {
    const body = text.trim()
    if (!body || disabled || busy) return
    setBusy(true)
    setError(null)
    onSend(body)
      .then(() => setText(''))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Send failed'))
      .finally(() => setBusy(false))
  }

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      {error && <div className="composer__error">{error}</div>}
      <textarea
        rows={2}
        value={text}
        disabled={disabled}
        placeholder={disabled ? 'Pick a conversation' : placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            submit()
          }
        }}
      />
      <button type="submit" className="btn primary" disabled={disabled || busy || !text.trim()}>
        Send
      </button>
    </form>
  )
}
