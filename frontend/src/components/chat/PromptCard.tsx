import { useState } from 'react'
import type { AskPayload, OpenPrompt } from '../../api'
import { useAnswer } from './useAnswer'
import './cards.css'

/** An open ASK: choice buttons, a text field, or Approve/Reject (also for
 * approve_account). need_password is answered by the backend on arrival, so
 * its card is informational only. */
export function PromptCard({ prompt, onAnswered }: { prompt: OpenPrompt; onAnswered: () => void }) {
  const p = prompt.payload as unknown as AskPayload
  const { busy, error, send } = useAnswer(prompt.id, onAnswered)
  const [remember, setRemember] = useState(true)
  const [text, setText] = useState(p.default ?? '')
  const inputId = `ask-${prompt.id}`

  const rememberBox = (
    <label className="qcard__remember">
      <input type="checkbox" checked={remember} disabled={busy} onChange={(e) => setRemember(e.target.checked)} />
      Remember this for future applications
    </label>
  )

  return (
    <div className="qcard">
      {p.kind === 'text' ? (
        <label htmlFor={inputId}>{p.question}</label>
      ) : (
        <div>{p.question}</div>
      )}
      {p.why && <div className="qcard__why">{p.why}</div>}

      {p.kind === 'choice' && (
        <>
          <div className="qcard__actions">
            {p.options.map((o) => (
              <button
                key={o}
                type="button"
                className={o === p.default ? 'btn qcard__default' : 'btn'}
                disabled={busy}
                onClick={() => send({ answer: o, remember })}
              >
                {o}
              </button>
            ))}
          </div>
          {rememberBox}
        </>
      )}

      {p.kind === 'text' && (
        <>
          <div className="qcard__actions">
            <input
              id={inputId}
              type="text"
              value={text}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
            />
            <button
              type="button"
              className="btn primary"
              disabled={busy || !text.trim()}
              onClick={() => send({ answer: text, remember })}
            >
              Send
            </button>
          </div>
          {rememberBox}
        </>
      )}

      {p.kind === 'approve' && (
        <div className="qcard__actions">
          <button type="button" className="btn primary" disabled={busy} onClick={() => send({ answer: 'approve' })}>
            Approve
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => send({ answer: 'reject' })}>
            Reject
          </button>
        </div>
      )}

      {p.kind === 'approve_account' && (
        <>
          <div className="kv">
            {p.domain && <div><span>Domain</span>{p.domain}</div>}
            {p.email && <div><span>Email</span>{p.email}</div>}
            {p.login_url && <div><span>Sign-up page</span>{p.login_url}</div>}
            {p.terms_summary && <div><span>Terms</span>{p.terms_summary}</div>}
            <div>
              <span>Browser is on</span>
              {p.page_urls?.length ? p.page_urls.join(', ') : 'unknown'}
            </div>
          </div>
          <div className="qcard__note">
            Approving creates this account and accepts these terms: the backend generates a
            password, types it in and submits the sign-up form (never shown to the agent), then
            saves it to Logins.
          </div>
          <div className="qcard__actions">
            <button type="button" className="btn primary" disabled={busy} onClick={() => send({ answer: 'approve' })}>
              Approve
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => send({ answer: 'reject' })}>
              Reject
            </button>
          </div>
        </>
      )}

      {p.kind === 'need_password' && (
        <>
          <div className="kv">
            {p.domain && <div><span>Domain</span>{p.domain}</div>}
          </div>
          <div className="qcard__note">
            The backend types the saved password in and submits the sign-in form when the browser
            is really on this site; the agent never sees it.
          </div>
        </>
      )}

      {error && <div className="qcard__error" role="alert">{error}</div>}
    </div>
  )
}
