import { useEffect, useState } from 'react'
import { ApiError, get, put } from '../api'
import '../components/ui.css'

// Types declared locally per the Phase B worktree convention (T13/T15/T17
// each declare their own types; Task 18 may lift shared ones into api.ts).

interface MemoryItem {
  id: number
  label: string
  answer: string
  kind: string | null
  options: string[] | null
  is_volatile: boolean
  last_confirmed_at: string | null
  use_count: number
  last_used_at: string | null
  is_preference: boolean
}

async function del(path: string): Promise<void> {
  const res = await fetch(path, { method: 'DELETE' })
  if (!res.ok) {
    const body = await res.json().catch(() => undefined)
    throw new ApiError(res.status, body?.message ?? `Request failed (${res.status})`, body)
  }
}

function Row({ item, onChanged }: { item: MemoryItem; onChanged: () => void }) {
  const [answer, setAnswer] = useState(item.answer)
  const [volatile, setVolatile] = useState(item.is_volatile)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dirty = answer !== item.answer || volatile !== item.is_volatile

  async function save() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await put<{ ok: boolean }>(`/api/memory/${item.id}`, { answer, is_volatile: volatile })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function confirmDelete() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await del(`/api/memory/${item.id}`)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <tr>
      <td>
        {item.label}
        {item.is_preference && <span className="banner banner--done" style={{
          display: 'inline-block', marginLeft: 'var(--space-2)', padding: '2px 8px',
        }}>preference</span>}
      </td>
      <td>
        <label htmlFor={`answer-${item.id}`} style={{ position: 'absolute', left: '-9999px' }}>
          Answer for {item.label}
        </label>
        <input id={`answer-${item.id}`} value={answer} disabled={busy}
              onChange={(e) => setAnswer(e.target.value)} />
      </td>
      <td>
        <label htmlFor={`volatile-${item.id}`}>
          <input id={`volatile-${item.id}`} type="checkbox" checked={volatile} disabled={busy}
                onChange={(e) => setVolatile(e.target.checked)} />{' '}
          Ask me again next time
        </label>
      </td>
      <td>
        {item.use_count} use{item.use_count === 1 ? '' : 's'}
        {item.last_used_at && <div className="hint">last: {item.last_used_at}</div>}
      </td>
      <td>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
          <button type="button" className="btn primary" disabled={busy || !dirty} onClick={save}>
            Save
          </button>
          {confirmingDelete ? (
            <>
              <button type="button" className="btn" disabled={busy} onClick={confirmDelete}>
                Confirm delete
              </button>
              <button type="button" className="btn" disabled={busy}
                     onClick={() => setConfirmingDelete(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn" disabled={busy}
                   onClick={() => setConfirmingDelete(true)}>
              Delete
            </button>
          )}
        </div>
        {error && <div className="field-error">{error}</div>}
      </td>
    </tr>
  )
}

export function Memory() {
  const [items, setItems] = useState<MemoryItem[] | null>(null)

  function load() {
    get<{ items: MemoryItem[] }>('/api/memory').then((r) => setItems(r.items))
  }

  useEffect(load, [])

  if (!items) return null

  return (
    <>
      <h1 className="page-title">Memory</h1>
      {items.length === 0 ? (
        <p className="rationale">
          Nothing remembered yet. Answers you give the agent (and mark "remember") show up here.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Question</th>
              <th>Answer</th>
              <th>Ask again?</th>
              <th>Used</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <Row key={item.id} item={item} onChanged={load} />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
