import { useState } from 'react'
import type { ConfirmPayload, OpenPrompt } from '../../api'
import { useAnswer } from './useAnswer'
import '../ui.css'
import './cards.css'

/** The final review before a send: Approve, edit individual answers, or
 * cancel the application. A change posts only the rows actually edited. */
export function ConfirmCard({ prompt, onAnswered }: { prompt: OpenPrompt; onAnswered: () => void }) {
  const p = prompt.payload as unknown as ConfirmPayload
  const { busy, error, send } = useAnswer(prompt.id, onAnswered)
  const [mode, setMode] = useState<'view' | 'edit' | 'cancel'>('view')
  // Edits keyed by row index; `changes` keeps only values that differ.
  const [edits, setEdits] = useState<Record<number, string>>({})
  const changes: Record<string, string> = {}
  p.fields.forEach((f, i) => {
    if (i in edits && edits[i] !== f.value) changes[f.label] = edits[i]
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
                {mode === 'edit' ? (
                  <input
                    type="text"
                    aria-label={f.label}
                    value={edits[i] ?? f.value}
                    disabled={busy}
                    onChange={(e) => setEdits({ ...edits, [i]: e.target.value })}
                  />
                ) : (
                  f.value
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
              disabled={busy || !changed}
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

      {error && <div className="qcard__error" role="alert">{error}</div>}
    </div>
  )
}
