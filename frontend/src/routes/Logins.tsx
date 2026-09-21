import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError, del, errorText, get, post } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { TextInput, inCls } from '../components/FormPage'
import { Icon, Spinner } from '../components/Icon'
import { Menu } from '../components/Menu'
import { TopBarActions, useShell } from '../components/shell'
import { P } from '../icons'
import { localDay } from '../time'
import './Logins.css'

// -- shapes /api/logins returns (credentials.list_): metadata only. The
// password is never returned, by this route or any other. --

interface Login {
  id: number
  domain: string
  login_url: string | null
  email: string
  created_by: 'agent' | 'user'
  created_at: string
  last_used_at: string | null
}

interface Draft {
  domain: string
  login_url: string
  email: string
  password: string
}

const EMPTY: Draft = { domain: '', login_url: '', email: '', password: '' }

/** Add login (LG1): an account you already have. The password is write-only —
 * it goes straight to credentials.put, encrypted, and nothing reads it back. */
function AddLogin({ onClose, onSaved }: { onClose: () => void; onSaved: (message: string) => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [alert, setAlert] = useState<string | null>(null)
  const [exists, setExists] = useState<{ message: string; created_by: string } | null>(null)
  const [reveal, setReveal] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const d = ref.current
    if (d && !d.open) d.showModal()   // StrictMode runs this twice
  }, [])

  const set = (patch: Partial<Draft>) => {
    setDraft({ ...draft, ...patch })
    setErrors((e) => {
      const next = { ...e }
      for (const k of Object.keys(patch)) delete next[k]
      return next
    })
  }

  const ready = draft.domain.trim() && draft.email.trim() && draft.password

  async function save(replace: boolean) {
    setBusy(true)
    setErrors({})
    setAlert(null)
    try {
      const r = await post<{ message: string }>('/api/logins', { ...draft, replace })
      onSaved(r.message)
    } catch (e: unknown) {
      const body = e instanceof ApiError ? (e.body as Record<string, unknown> | undefined) : undefined
      if (e instanceof ApiError && e.status === 422 && body?.errors) {
        setErrors(body.errors as Record<string, string>)
      } else if (e instanceof ApiError && e.status === 409 && body?.exists) {
        // put() upserts by domain, so replacing is always a second, explicit yes.
        setExists({ message: e.message, created_by: String(body.created_by) })
      } else {
        // Including the missing-CREDENTIAL_KEY 409, where nothing was written.
        setAlert(errorText(e))
      }
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <dialog ref={ref} className="dialog add-login" aria-labelledby="add-login-title"
            onCancel={(e) => {
              e.preventDefault()
              onClose()
            }}>
      {exists ? (
        <>
          <div className="dialog__body">
            <span className="dialog__icon"><Icon d={P.alert} size={18} /></span>
            <div>
              <h2 id="add-login-title" className="dialog__title">{exists.message}</h2>
              <p className="dialog__text">
                It was created by {exists.created_by === 'agent' ? 'the agent, with your approval' : 'you'}.
                Replace its email and password? The old password can't be recovered.
              </p>
            </div>
          </div>
          <div className="dialog__actions">
            <button type="button" className="btn ghost" disabled={busy}
                    onClick={() => setExists(null)}>Cancel</button>
            <button type="button" className="btn danger" disabled={busy}
                    onClick={() => save(true)}>{busy ? <Spinner /> : null}Replace</button>
          </div>
        </>
      ) : (
        <>
          <div className="add-login__head">
            <h2 id="add-login-title" className="dialog__title">Add login</h2>
            <p className="dialog__text">
              For an account you already have. The password can't be viewed after saving.
            </p>
          </div>

          <div className="add-login__body">
            {alert && (
              <div className="alert" role="alert">
                <Icon d={P.refused} size={16} />
                <span>{alert}</span>
              </div>
            )}
            <TextInput id="login-domain" label="Domain" required mono value={draft.domain}
                       onChange={(v) => set({ domain: v })} error={errors.domain}
                       placeholder="careers.example.com" disabled={busy} />
            <TextInput id="login-url" label="Sign-in page URL" value={draft.login_url}
                       onChange={(v) => set({ login_url: v })} error={errors.login_url}
                       placeholder="https:// (optional)" disabled={busy} />
            <TextInput id="login-email" label="Email" required type="email" value={draft.email}
                       onChange={(v) => set({ email: v })} error={errors.email} disabled={busy} />

            <div className="f">
              <label className="fl add-login__pw-label" htmlFor="login-password">
                Password<span className="req"> *</span>
                <span className="mono add-login__tag">Write-only</span>
              </label>
              <span className="add-login__pw">
                <input id="login-password" className={inCls(errors.password)} disabled={busy}
                       type={reveal ? 'text' : 'password'} autoComplete="new-password"
                       value={draft.password} onChange={(e) => set({ password: e.target.value })} />
                <button type="button" className="ib" aria-pressed={reveal} disabled={busy}
                        aria-label={reveal ? 'Hide password' : 'Show password'}
                        onClick={() => setReveal((r) => !r)}>
                  <Icon d={P.eye} size={16} />
                </button>
              </span>
              {errors.password && <span className="err">{errors.password}</span>}
            </div>
          </div>

          <div className="dialog__actions">
            <button type="button" className="btn ghost" disabled={busy} onClick={onClose}>Cancel</button>
            <button type="button" className="btn pri" disabled={busy || !ready}
                    onClick={() => save(false)}>{busy ? <Spinner /> : null}Save login</button>
          </div>
        </>
      )}
    </dialog>,
    document.body,
  )
}

/** Logins (approved Screen 12): the site accounts the backend signs in with.
 * The agent never holds a password — its prompt lists domain and email only,
 * and secret_fill types the secret in a CDP session the agent can't read. */
export function Logins() {
  const { toast } = useShell()
  const [items, setItems] = useState<Login[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [adding, setAdding] = useState(false)
  const [doomed, setDoomed] = useState<Login | null>(null)
  const [busy, setBusy] = useState(false)
  const [rowError, setRowError] = useState<Record<number, string>>({})

  const load = useCallback(
    () =>
      get<{ items: Login[] }>('/api/logins')
        .then((d) => {
          setItems(d.items)
          setLoadError(null)
        })
        .catch((e: unknown) => setLoadError(errorText(e))),
    [],
  )

  useEffect(() => {
    load()
  }, [load])

  async function remove(login: Login) {
    setBusy(true)
    try {
      await del(`/api/logins/${login.id}`)
      setDoomed(null)
      await load()
      toast({ text: `Login for ${login.domain} deleted` })
    } catch (e: unknown) {
      setDoomed(null)
      setRowError({ [login.id]: errorText(e) })
    } finally {
      setBusy(false)
    }
  }

  if (!items) {
    return (
      <div className="page logins">
        <div className="page-head"><h1>Logins</h1></div>
        {loadError ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load your logins:</b> {loadError}</span>
            <button type="button" className="btn logins__retry" onClick={load}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="card logins__skel">
            {[0, 1, 2].map((i) => (
              <div className="logins__skel-row" key={i}>
                <span className="skel" style={{ width: '30%' }} />
                <span className="skel" style={{ width: '40%' }} />
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  const q = filter.trim().toLowerCase()
  const shown = q ? items.filter((l) => `${l.domain} ${l.email}`.toLowerCase().includes(q)) : items

  return (
    <div className="page logins">
      <TopBarActions>
        <button type="button" className="btn" onClick={() => setAdding(true)}>
          <Icon d={P.plus} width={2.5} />Add login
        </button>
      </TopBarActions>

      <div className="page-head">
        <h1>Logins</h1>
        <p>Site accounts the backend uses to sign in or create accounts during applications.</p>
      </div>

      <div className="notice logins__note">
        <Icon d={P.lock} size={18} />
        <span>
          Passwords are encrypted on this machine with your <code>CREDENTIAL_KEY</code>. The backend
          types them in only when the browser is really on that site — they are never shown to the
          agent, and never shown here.
        </span>
      </div>

      <section className="card logins__card">
        {items.length > 0 && (
          <div className="ch">
            <span className="logins__title">
              <h2 className="ct">Saved logins</h2>
              <span className="mono faint">{items.length}</span>
            </span>
            <span className="searchbox logins__filter">
              <Icon d={P.search} />
              <input className="field" value={filter} onChange={(e) => setFilter(e.target.value)}
                     placeholder="Filter by site or email" aria-label="Filter logins" />
            </span>
          </div>
        )}

        {items.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.key} size={16} /></span>
            No saved logins
            <span className="help">
              When a site needs an account the agent asks you in chat first; approved logins appear here.
            </span>
            <button type="button" className="btn" onClick={() => setAdding(true)}>
              <Icon d={P.plus} width={2.5} />Add login
            </button>
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            No logins match
            <button type="button" className="btn" onClick={() => setFilter('')}>Clear filter</button>
          </div>
        ) : (
          <table className="logins-table">
            <thead>
              <tr>
                <th className="th">Site</th>
                <th className="th">Email</th>
                <th className="th">Created by</th>
                <th className="th c-created">Created</th>
                <th className="th c-used">Last used</th>
                <th className="th c-actions" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {shown.map((l) => (
                <tr key={l.id}>
                  <td className="td c-site">
                    {l.login_url ? (
                      <a className="mono" href={l.login_url} target="_blank" rel="noreferrer">{l.domain}</a>
                    ) : (
                      <span className="mono">{l.domain}</span>
                    )}
                  </td>
                  <td className="td c-email">
                    <span className="logins__email">{l.email}</span>
                    {rowError[l.id] && <span className="err">{rowError[l.id]}</span>}
                  </td>
                  <td className="td c-by">
                    <span className="b sl">{l.created_by === 'agent' ? 'Agent · you approved' : 'You'}</span>
                  </td>
                  <td className="td c-created mono faint">{localDay(l.created_at)}</td>
                  <td className="td c-used mono faint">
                    {l.last_used_at ? localDay(l.last_used_at) : <span className="never">Never</span>}
                  </td>
                  <td className="td c-actions">
                    <button type="button" className="btn sm ghost rose-text logins__delete"
                            onClick={() => setDoomed(l)}>Delete</button>
                    <span className="logins__menu">
                      <Menu items={[
                        { label: 'Delete', icon: P.trash, danger: true, onSelect: () => setDoomed(l) },
                      ]} />
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {adding && (
        <AddLogin onClose={() => setAdding(false)}
                  onSaved={(message) => {
                    setAdding(false)
                    load()
                    toast({ text: `${message} — the password can't be viewed again` })
                  }} />
      )}

      {doomed && (
        <ConfirmDialog
          title={`Delete the login for ${doomed.domain}?`}
          text="The agent will need your approval to create or sign in to an account there again."
          confirm="Delete" busy={busy}
          onConfirm={() => remove(doomed)} onCancel={() => setDoomed(null)} />
      )}
    </div>
  )
}
