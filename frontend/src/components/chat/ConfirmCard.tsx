import { useState } from 'react'
import { Icon } from '../Icon'
import { P } from '../../icons'
import { useAnswer } from './useAnswer'
import type { ConfirmPayload, OpenPrompt } from '../../api'

// Mirrors store._SECRET_QA_RE for display only: the backend is the authority and
// refuses a change to such a field.
const SECRET_LABEL =
  /password(?!\s*manager)|passcode|passphrase|verification code|security code|one[- ]time (code|password)|\b2fa\b|contraseñ|mot de passe|kennwort|पासवर्ड|\bssn\b|social security|\bpan\b|aadhaar|\botp\b|\bcvv\b|\bpin\b(?!\s*code)|bank account|card number/i

/** The final review before a send: Approve, edit individual answers, or
 * cancel the application. A change posts only the rows actually edited, and
 * a secret row is never editable here — the backend fills those from Logins
 * and refuses a change to one. */
export function ConfirmCard({ prompt, onAnswered, mode: runMode, canSubmit }: {
  prompt: OpenPrompt
  onAnswered: () => void
  /** The run's mode (manual/auto), shown as the header badge. */
  mode?: string | null
  /** SUBMISSION_IMPLEMENTED: false means approving saves a draft. */
  canSubmit?: boolean
}) {
  const p = prompt.payload as unknown as ConfirmPayload
  const { busy, error, send } = useAnswer(prompt.id, onAnswered)
  const [mode, setMode] = useState<'view' | 'edit' | 'cancel'>('view')
  // Edits keyed by row index; `changes` keeps only trimmed values that differ.
  // A cleared row blocks Save: the backend only checks `changes` is non-empty,
  // so an empty value would overwrite a real answer.
  const [edits, setEdits] = useState<Record<number, string>>({})
  const changes: Record<string, string> = {}
  let blank = false
  p.fields.forEach((f, i) => {
    if (!(i in edits)) return
    const v = edits[i].trim()
    if (v === f.value) return
    if (!v) blank = true
    else changes[f.label] = v
  })
  const changed = Object.keys(changes).length > 0

  return (
    <section className="ccard card">
      <div className="ccard__head ccard__head--review">
        <span className="ccard__head-l">
          <Icon d={P.transcript} size={16} />
          Review before applying
        </span>
        <span className="ccard__head-r">
          {runMode && <span className="mono ccard__mode">{runMode}</span>}
          {!canSubmit && (
            <span className="b sl">
              <Icon d={P.shield} size={11} />
              Submission off — approving saves a draft
            </span>
          )}
        </span>
      </div>

      <div className="rows">
        {p.fields.map((f, i) => {
          const secret = SECRET_LABEL.test(f.label)
          return (
            <div key={i} className={secret ? 'row row--secret' : 'row'}>
              <span className="row__k">
                {secret && <Icon d={P.lock} size={12} />}
                {f.label}
              </span>
              {mode === 'edit' && !secret ? (
                <input className="field" type="text" aria-label={f.label} disabled={busy}
                       value={edits[i] ?? f.value}
                       onChange={(e) => setEdits({ ...edits, [i]: e.target.value })} />
              ) : (
                <span className={secret ? 'row__secret' : undefined}>
                  {secret ? 'Filled by the backend from Logins — never shown' : f.value}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {(p.files?.length || p.account_actions?.length || p.memory_used?.length) && (
        <div className="ccard__grid">
          <div>
            <span className="lbl">Files</span>
            {p.files?.length
              ? p.files.map((f) => <span key={f} className="mono ccard__file">{f}</span>)
              : <span className="muted-text">None</span>}
          </div>
          <div>
            <span className="lbl">Account actions</span>
            {p.account_actions?.length
              ? p.account_actions.map((a) => <span key={a}>{a}</span>)
              : <span className="muted-text">None</span>}
          </div>
          <div>
            <span className="lbl">Memory used</span>
            <span className="mems">
              {p.memory_used?.length
                ? p.memory_used.map((m) => <span key={m} className="mem mono">{m}</span>)
                : <span className="muted-text">None</span>}
            </span>
          </div>
        </div>
      )}

      {p.notes && <p className="ccard__notes">{p.notes}</p>}

      <div className="ccard__foot">
        {mode === 'view' && (
          <div className="ccard__actions">
            <button className="btn pri" disabled={busy}
                    onClick={() => send({ decision: 'approve' })}>Approve</button>
            <button className="btn" disabled={busy}
                    onClick={() => setMode('edit')}>Change an answer</button>
            <button className="btn ghost rose-text" disabled={busy}
                    onClick={() => setMode('cancel')}>Cancel application</button>
          </div>
        )}

        {mode === 'edit' && (
          <>
            <div className="ccard__actions">
              <button className="btn pri" disabled={busy || !changed || blank}
                      onClick={() => send({ decision: 'change', changes })}>Save changes</button>
              <button className="btn" disabled={busy}
                      onClick={() => { setEdits({}); setMode('view') }}>Discard</button>
            </div>
            {blank && (
              <p className="ccard__hint">
                A changed answer can't be empty — use Discard to keep the original.
              </p>
            )}
            <p className="ccard__hint">Secrets are never typed here.</p>
          </>
        )}

        {mode === 'cancel' && (
          <>
            <p className="ccard__sub">Cancel this application? The job will be marked cancelled.</p>
            <div className="ccard__actions">
              <button className="btn danger" disabled={busy}
                      onClick={() => send({ decision: 'cancel' })}>Yes, cancel</button>
              <button className="btn" disabled={busy}
                      onClick={() => setMode('view')}>Keep it</button>
            </div>
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
