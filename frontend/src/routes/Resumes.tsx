import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { errorText, uploadFile, type ActionResult } from '../api'
import { Icon, Spinner } from '../components/Icon'
import { TopBarActions } from '../components/shell'
import { usePoll } from '../components/usePoll'
import { P } from '../icons'
import { dateTime } from '../time'
import './Resumes.css'

// The master template tailoring renders from, and every version it has
// produced. One version per job, created on that job's first Apply.

interface Bullet {
  text: string
  fact_ids: number[]
  /** null where a citation no longer resolves (RS1, as the job hub does). */
  fact_claims: (string | null)[]
}

interface Version {
  version: string
  created_at: string
  job_id: number
  company: string
  title: string
  summary: string
  bullets: Bullet[]
  prompt_version: string | null
}

interface ResumesData {
  master: { exists: boolean; path: string; modified?: string }
  versions: Version[]
}

/** The bullets and their fact chips, the same markup the job hub renders. */
function Bullets({ summary, bullets }: { summary: string; bullets: Bullet[] }) {
  return (
    <>
      {summary && <p className="why-text vexp__summary">{summary}</p>}
      <ul className="bullets">
        {bullets.map((b, i) => (
          <li key={i}>
            <span>{b.text}</span>
            {b.fact_ids.length > 0 && (
              <span className="facts">
                {b.fact_ids.map((f, j) => {
                  const claim = b.fact_claims[j]
                  return claim == null ? (
                    <span className="fact fact--gone" key={f}>
                      <span className="mono">#{f}</span>Unknown fact
                    </span>
                  ) : (
                    <Link className="fact" key={f} to={`/facts?fact=${f}`} title={claim}>
                      <span className="mono">#{f}</span>{claim}
                    </Link>
                  )
                })}
              </span>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

function MasterCard({ master, result, onPick, busy, onFile }: {
  master: ResumesData['master']
  result: ActionResult | null
  onPick: () => void
  busy: boolean
  onFile: (file: File) => void
}) {
  // The card itself is the drop target -- a separate dropzone would be a
  // second upload control saying the same thing as the top bar's button.
  const [over, setOver] = useState(false)
  const dz = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault()
      setOver(true)
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault()
      setOver(false)
      const file = e.dataTransfer.files?.[0]
      if (file && !busy) onFile(file)
    },
  }
  const cls = (base: string) => (over ? `${base} master--over` : base)

  if (!master.exists) {
    return (
      <section className={cls('card master master--missing')} {...dz}>
        <div className="master__id">
          <span className="master__tile"><Icon d={P.doc} size={20} /></span>
          <div className="master__text">
            <span className="ct">No master resume</span>
            <p className="master__sub">
              Tailoring and Apply can't run until you upload one. It goes at{' '}
              <span className="mono">{master.path}</span>. Drop a <b>.docx</b> here, or:
            </p>
          </div>
        </div>
        <div className="master__act">
          <button className="btn pri" disabled={busy} onClick={onPick}>
            {busy ? <Spinner /> : <Icon d={P.upload} size={14} />}Upload master
          </button>
        </div>
        {result && <UploadResult result={result} />}
      </section>
    )
  }

  return (
    <section className={cls('card master')} {...dz}>
      <div className="master__id">
        <span className="master__tile"><Icon d={P.doc} size={20} /></span>
        <div className="master__text">
          <span className="master__head">
            <span className="ct">Master resume</span>
            <span className="b em">Ready for tailoring</span>
          </span>
          <p className="master__sub mono">
            {master.path}
            {master.modified && <> · modified {dateTime(master.modified.replace('T', ' '))}</>}
          </p>
          {/* A rule worth saying plainly, not only after an upload. */}
          <p className="master__note">
            Replacing the master affects future tailoring only — existing versions keep
            the file they were rendered from. Drop a <b>.docx</b> on this card to replace it.
          </p>
        </div>
      </div>
      {result && <UploadResult result={result} />}
      <details className="tmpl">
        <summary><Icon d={P.chevronRight} size={14} />How the template works</summary>
        <p>
          Tailoring fills two marker paragraphs in your document:{' '}
          <span className="mono">&lt;&lt;SUMMARY&gt;&gt;</span> becomes the summary, and{' '}
          <span className="mono">&lt;&lt;PROJECT_BULLET&gt;&gt;</span> is cloned once per tailored
          bullet. Everything else — your layout, header, education, styling — is left
          untouched. A file without the markers is prepared automatically on upload, and
          what changed is listed above.
        </p>
      </details>
    </section>
  )
}

function UploadResult({ result }: { result: ActionResult }) {
  if (!result.ok) {
    return (
      <div className="alert master__result" role="alert">
        <Icon d={P.refused} size={16} />
        <span>{result.message}</span>
      </div>
    )
  }
  return (
    <div className="master__ok" role="status">
      <p className="master__ok-head"><Icon d={P.check} size={14} />{result.message}</p>
      {result.changes && result.changes.length > 0 && (
        <>
          <p className="master__ok-sub">What we changed in your file</p>
          <ul className="master__changes">
            {result.changes.map((c) => <li key={c} className="mono">{c}</li>)}
          </ul>
        </>
      )}
    </div>
  )
}

function VersionRow({ v, open, onToggle }: {
  v: Version
  open: boolean
  onToggle: () => void
}) {
  return (
    <>
      <div className={open ? 'vrow is-open' : 'vrow'}>
        <button className="chev" aria-expanded={open} aria-label={`Bullets in ${v.version}`}
                onClick={onToggle}>
          <Icon d={open ? P.chevronDown : P.chevronRight} />
        </button>
        <Link className="vrow__job" to={`/jobs/${v.job_id}`}>{v.company} · {v.title}</Link>
        <span className="mono vrow__dim">{v.version}</span>
        <span className="mono vrow__dim">{v.created_at.slice(0, 10)}</span>
        <span className="mono vrow__dim">{v.bullets.length}</span>
        <span className="mono vrow__dim">{v.prompt_version ?? '—'}</span>
        {/* A real anchor: the download is a FileResponse, not JSON. */}
        <a className="vrow__dl" href={`/resume/${v.version}`}>Download</a>
      </div>
      {open && (
        <div className="vexp">
          <p className="vexp__meta mono">
            {v.version} · {v.created_at.slice(0, 10)} · {v.bullets.length} bullets
            {v.prompt_version && <> · {v.prompt_version}</>}
          </p>
          <Bullets summary={v.summary} bullets={v.bullets} />
        </div>
      )}
    </>
  )
}

export function Resumes() {
  const { data, error, reload } = usePoll<ResumesData>('/api/resumes', 0)
  const fileRef = useRef<HTMLInputElement>(null)
  const [result, setResult] = useState<ActionResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const [q, setQ] = useState('')

  const pick = () => fileRef.current?.click()

  const send = (file: File) => {
    setBusy(true)
    setResult(null)
    uploadFile<ActionResult>('/api/resumes', file)
      .then((r) => {
        setResult(r)
        reload()
      })
      .catch((err: unknown) => setResult({ ok: false, message: errorText(err) }))
      .finally(() => setBusy(false))
  }

  const upload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // Cleared on every attempt, not just on success: re-picking the same file
    // after a refusal fires no change event otherwise, and the retry is lost.
    e.target.value = ''
    if (file) send(file)
  }

  const picker = (
    <input ref={fileRef} className="rsm__file" type="file" accept=".docx"
           onChange={upload} tabIndex={-1} aria-hidden="true" />
  )

  if (!data) {
    return (
      <div className="page rsm">
        <div className="page-head"><h1>Resumes</h1></div>
        {error ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load resumes:</b> {error}</span>
            <button className="btn sm" onClick={reload}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="rsm__skel">
            <div className="skel master-skel" />
            <div className="skel list-skel" />
          </div>
        )}
      </div>
    )
  }

  const { master, versions } = data
  const needle = q.trim().toLowerCase()
  const shown = needle
    ? versions.filter((v) => `${v.company} ${v.title}`.toLowerCase().includes(needle))
    : versions

  return (
    <div className="page rsm">
      <TopBarActions>
        <button className="btn" disabled={busy} onClick={pick}>
          {busy ? <Spinner /> : <Icon d={P.upload} size={14} />}
          {master.exists ? 'Replace master' : 'Upload master'}
        </button>
      </TopBarActions>
      {picker}

      <div className="page-head">
        <h1>Resumes</h1>
        <p>The master template every tailored resume is rendered from.</p>
      </div>

      <MasterCard master={master} result={result} onPick={pick} busy={busy} onFile={send} />

      <section className="card">
        <div className="ch vch">
          <span className="vch__title">
            <span className="ct">Tailored versions</span>
            <span className="mono vrow__dim">{versions.length}</span>
            <span className="vch__note">· one per job, reused on Redo draft and Continue</span>
          </span>
          {versions.length > 0 && (
            <label className="vfilter">
              <Icon d={P.search} size={14} />
              <input className="field" type="search" value={q}
                     placeholder="Filter by company or role"
                     aria-label="Filter by company or role"
                     onChange={(e) => setQ(e.target.value)} />
            </label>
          )}
        </div>

        {versions.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.file} size={20} /></span>
            <p>No tailored resumes yet — one is created the first time you Apply to a job</p>
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.search} size={20} /></span>
            <p>No versions match this filter</p>
            <button className="btn" onClick={() => setQ('')}>Clear filter</button>
          </div>
        ) : (
          <>
            <div className="vrow vrow--head">
              <span />
              <span className="lbl">Job</span>
              <span className="lbl">Version</span>
              <span className="lbl">Generated</span>
              <span className="lbl">Bullets</span>
              <span className="lbl">Prompt</span>
              <span />
            </div>
            {shown.map((v) => (
              <VersionRow key={v.version} v={v} open={open === v.version}
                          onToggle={() => setOpen(open === v.version ? null : v.version)} />
            ))}
            {shown.length !== versions.length && (
              <p className="vch__count">
                Showing <span className="mono">{shown.length}</span> of{' '}
                <span className="mono">{versions.length}</span>
              </p>
            )}
          </>
        )}
      </section>
    </div>
  )
}
