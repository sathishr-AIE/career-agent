import { useEffect, useState } from 'react'
import { ApiError, get } from '../api'

// Types declared locally. The API never returns a password.

interface Login {
  id: number
  domain: string
  login_url: string | null
  email: string
  created_by: 'agent' | 'user'
  created_at: string
  last_used_at: string | null
}

async function del(path: string): Promise<void> {
  const res = await fetch(path, { method: 'DELETE' })
  if (!res.ok) {
    const body = await res.json().catch(() => undefined)
    throw new ApiError(res.status, body?.message ?? `Request failed (${res.status})`, body)
  }
}

function Row({ login, onChanged }: { login: Login; onChanged: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirmDelete() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await del(`/api/logins/${login.id}`)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <tr>
      <td>
        {login.login_url ? (
          <a href={login.login_url} target="_blank" rel="noreferrer">{login.domain}</a>
        ) : (
          login.domain
        )}
      </td>
      <td>{login.email}</td>
      <td>{login.created_by === 'agent' ? 'Agent (you approved)' : 'You'}</td>
      <td>
        {login.created_at}
        {login.last_used_at && <div className="hint">last used: {login.last_used_at}</div>}
      </td>
      <td>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
          {confirming ? (
            <>
              <button type="button" className="btn" disabled={busy} onClick={confirmDelete}>
                Confirm delete
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn" disabled={busy} onClick={() => setConfirming(true)}>
              Delete
            </button>
          )}
        </div>
        {error && <div className="field-error">{error}</div>}
      </td>
    </tr>
  )
}

export function Logins() {
  const [items, setItems] = useState<Login[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  function load() {
    get<{ items: Login[] }>('/api/logins')
      .then((r) => {
        setLoadError(null)
        setItems(r.items)
      })
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [])

  if (loadError) {
    return (
      <>
        <h1 className="page-title">Logins</h1>
        <div className="banner banner--denied">Couldn't load logins: {loadError}</div>
      </>
    )
  }

  if (!items) return null

  return (
    <>
      <h1 className="page-title">Logins</h1>
      {items.length === 0 ? (
        <p className="rationale">
          No saved logins. When a site needs an account, the agent asks you in chat first; approved
          logins show up here.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Site</th>
              <th>Email</th>
              <th>Created by</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((login) => (
              <Row key={login.id} login={login} onChanged={load} />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
