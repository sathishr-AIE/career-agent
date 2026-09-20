import { useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { Icon, Spinner } from '../Icon'
import { P } from '../../icons'

/** The chat composer: a textarea, a toolbar slot (Home puts the model
 * picker there) and an icon-only Send. It owns no error state -- a refusal
 * belongs above the composer, where the page renders it -- and it clears the
 * text only once the send resolves, so a refused message stays editable. */
export function Composer({ onSend, placeholder = 'Message the agent', toolbar, helper,
                          invalid = false, disabled = false }: {
  onSend: (text: string) => Promise<unknown>
  placeholder?: string
  toolbar?: ReactNode
  helper?: ReactNode
  invalid?: boolean
  disabled?: boolean
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    const body = text.trim()
    if (!body || busy || disabled) return
    setBusy(true)
    onSend(body)
      .then(() => {
        setText('')
        if (box.current) box.current.style.height = ''
      })
      .catch(() => {})            // the page shows why; the text stays put
      .finally(() => setBusy(false))
  }

  const keys = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <form className={`cbar card${invalid ? ' cbar--invalid' : ''}`} onSubmit={submit}>
      <textarea
        ref={box}
        className="cbar__text"
        rows={2}
        value={text}
        placeholder={placeholder}
        disabled={disabled}
        onKeyDown={keys}
        onChange={(e) => {
          setText(e.target.value)
          const el = e.target
          el.style.height = ''                                   // measure, then grow to fit
          el.style.height = `${Math.min(el.scrollHeight, 160)}px`
        }}
      />
      {helper && <div className="cbar__helper">{helper}</div>}
      <div className="cbar__tools">
        {toolbar}
        <button className="btn pri cbar__send" type="submit"
                disabled={disabled || busy || !text.trim()} aria-label="Send">
          {busy ? <Spinner /> : <Icon d={P.up} size={16} />}
        </button>
      </div>
    </form>
  )
}
