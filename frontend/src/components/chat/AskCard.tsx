import { useState } from 'react'
import { Icon } from '../Icon'
import { P } from '../../icons'
import { useAnswer } from './useAnswer'
import type { AskPayload, OpenPrompt } from '../../api'

/** need_password is answered by the backend the moment it arrives, so it is
 * a notice rather than a card: nothing here can fill or reveal a password. */
function SigningIn({ domain }: { domain?: string }) {
  return (
    <div className="signin" role="status">
      <Icon d={P.lock} size={16} />
      <span>
        Signing in to <span className="mono">{domain ?? 'this site'}</span> with your saved
        login — the agent never sees the password.
      </span>
    </div>
  )
}

/** One open ASK from the agent: a choice, a typed answer, a plain approval,
 * or an account to create. A refusal shows inside the card and leaves it
 * open, because the backend refused — nothing was answered. */
export function AskCard({ prompt, onAnswered }: { prompt: OpenPrompt; onAnswered: () => void }) {
  const p = prompt.payload as unknown as AskPayload
  const { busy, error, send } = useAnswer(prompt.id, onAnswered)
  const [remember, setRemember] = useState(true)
  const [text, setText] = useState(p.default ?? '')
  const inputId = `ask-${prompt.id}`
  const account = p.kind === 'approve_account'

  if (p.kind === 'need_password') return <SigningIn domain={p.domain} />

  const rememberBox = (
    <label className="ccard__remember">
      <input type="checkbox" checked={remember} disabled={busy}
             onChange={(e) => setRemember(e.target.checked)} />
      Remember this for future applications
    </label>
  )

  const yesNo = (
    <div className="ccard__actions">
      <button className="btn pri" disabled={busy} onClick={() => send({ answer: 'approve' })}>
        Approve
      </button>
      <button className="btn" disabled={busy} onClick={() => send({ answer: 'reject' })}>
        Reject
      </button>
    </div>
  )

  return (
    <section className="ccard card">
      <div className="ccard__head ccard__head--ask">
        <Icon d={account ? P.userPlus : P.chat} size={14} />
        {account ? 'Create an account?' : 'Agent asks'}
      </div>
      <div className="ccard__body">
        {p.kind === 'text'
          ? <label className="ccard__q" htmlFor={inputId}>{p.question}</label>
          : <p className="ccard__q">{p.question}</p>}
        {p.why && <p className="ccard__sub">Why: {p.why}</p>}

        {p.kind === 'choice' && (
          <>
            <div className="ccard__actions">
              {p.options.map((o) => (
                // The agent's own suggestion is outlined, not filled: it is a
                // default, not the primary action.
                <button key={o} className={o === p.default ? 'btn ccard__default' : 'btn'}
                        disabled={busy} onClick={() => send({ answer: o, remember })}>
                  {o}
                </button>
              ))}
            </div>
            {rememberBox}
          </>
        )}

        {p.kind === 'text' && (
          <>
            <div className="ccard__row">
              <input id={inputId} className="field" type="text" value={text} disabled={busy}
                     onChange={(e) => setText(e.target.value)} />
              <button className="btn pri" disabled={busy || !text.trim()}
                      onClick={() => send({ answer: text.trim(), remember })}>Send</button>
            </div>
            {rememberBox}
          </>
        )}

        {p.kind === 'approve' && yesNo}

        {account && (
          <>
            <div className="kv">
              {p.domain && <><span className="kv__k">Domain</span><span className="mono">{p.domain}</span></>}
              {p.email && <><span className="kv__k">Email</span><span>{p.email}</span></>}
              {p.login_url && <><span className="kv__k">Sign-up page</span><span className="mono">{p.login_url}</span></>}
              <span className="kv__k">Browser is on</span>
              <span className="mono">{p.page_urls?.length ? p.page_urls.join(', ') : 'unknown'}</span>
              {p.terms_summary && <><span className="kv__k">Terms</span><span>{p.terms_summary}</span></>}
            </div>
            <p className="ccard__warn">
              Approving creates this account and accepts these terms. The backend generates a
              password, types it in, submits the form and saves it to Logins — the agent never
              sees it.
            </p>
            {yesNo}
          </>
        )}

        {error && (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={14} />
            <span><b>Refused:</b> {error}</span>
          </div>
        )}
      </div>
    </section>
  )
}
