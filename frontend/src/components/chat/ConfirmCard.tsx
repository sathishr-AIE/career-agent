import { useState } from 'react'
import type { ConfirmPayload, OpenPrompt } from '../../api'
import { useAnswer } from './useAnswer'
import './cards.css'

// Mirrors store._SECRET_QA_RE for display only: the backend is the authority and
// refuses a change to such a field.
const SECRET_LABEL =
  /password(?!\s*manager)|passcode|passphrase|verification code|security code|one[- ]time (code|password)|\b2fa\b|contraseñ|mot de passe|kennwort|पासवर्ड|\bssn\b|social security|\bpan\b|aadhaar|\botp\b|\bcvv\b|\bpin\b(?!\s*code)|bank account|card number/i

/** The final review before a send: Approve, edit individual answers, or
 * cancel the application. A change posts only the rows actually edited. */
export function ConfirmCard({ prompt, onAnswered }: { prompt: OpenPrompt; onAnswered: () => void }) {
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
    <div className="qcard">
      <h3>Review before applying</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {p.fields.map((f, i) => (
            <tr key={i}>
              <th scope="row">{f.label}</th>
              <td>
                {mode === 'edit' && !SECRET_LABEL.test(f.label) ? (
                  <input
                    type="text"
                    aria-label={f.label}
                    value={edits[i] ?? f.value}
                    disabled={busy}
                    onChange={(e) => setEdits({ ...edits, [i]: e.target.value })}
                  />
                ) : (
                  <>
                    {f.value}
                    {mode === 'edit' && (
                      <div className="qcard__note">
                        Secrets are never typed here — saved logins are filled by the backend.
                      </div>
                    )}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {p.files.length > 0 && (
        <div>
          Files
          <ul>{p.files.map((x, i) => <li key={i}>{String(x)}</li>)}</ul>
        </div>
      )}
      {p.account_actions.length > 0 && (
        <div>
          Account actions
          <ul>{p.account_actions.map((x, i) => <li key={i}>{String(x)}</li>)}</ul>
        </div>
      )}
      {p.notes && <div className="qcard__note">{p.notes}</div>}

      <div className="qcard__actions">
        {mode === 'view' && (
          <>
            <button type="button" className="btn primary" disabled={busy} onClick={() => send({ decision: 'approve' })}>
              Approve
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode('edit')}>
              Change an answer
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode('cancel')}>
              Cancel application
            </button>
          </>
        )}
        {mode === 'edit' && (
          <>
            <button
              type="button"
              className="btn primary"
              disabled={busy || !changed || blank}
              onClick={() => send({ decision: 'change', changes })}
            >
              Save changes
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => {
                setEdits({})
                setMode('view')
              }}
            >
              Discard
            </button>
          </>
        )}
        {mode === 'cancel' && (
          <>
            <span>Cancel this application? The job will be marked cancelled.</span>
            <button type="button" className="btn primary" disabled={busy} onClick={() => send({ decision: 'cancel' })}>
              Yes, cancel
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode('view')}>
              Keep it
            </button>
          </>
        )}
      </div>

      {mode === 'edit' && blank && (
        <div className="qcard__error">A changed answer can't be empty — use Discard to keep the original.</div>
      )}
      {error && <div className="qcard__error" role="alert">{error}</div>}
    </div>
  )
}
