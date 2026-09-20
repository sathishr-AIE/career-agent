import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorText, get, uploadFile } from '../api'
import { Icon, Spinner } from '../components/Icon'
import { TopBarActions, useShell } from '../components/shell'
import { P } from '../icons'
import './Resumes.css'

// -- shapes /api/resumes returns (context.resumes_context; RS1 adds job_id and
// prompt_version). Its timestamps are already local time, not naive UTC. --

interface Bullet {
  text: string
  fact_ids: number[]
  fact_claims: string[]
}

interface Version {
  version: string
  created_at: string
  company: string
  title: string
  summary: string
  bullets: Bullet[]
  job_id: number | null
  prompt_version: string | null
}

interface ResumesContext {
  master: { exists: boolean; path: string; modified?: string }
  versions: Version[]
}

interface UploadResult {
  ok: boolean
  message: string
  changes?: string[]
}

/** The two literal marker paragraphs tailor.render_docx fills. */
const MARKERS = ['<<SUMMARY>>', '<<PROJECT_BULLET>>']

const day = (local: string) => local.slice(0, 10)
const minute = (local: string) => local.replace('T', ' ').slice(0, 16)

function Dropzone({ busy, onFile }: { busy: boolean; onFile: (f: File) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const take = (files: FileList | null | undefined) => {
    const file = files?.[0]
    if (file) onFile(file)
  }
  return (
    <div className={`drop${over ? ' drop--over' : ''}`}
         onDragOver={(e) => {
           e.preventDefault()
           setOver(true)
         }}
         onDragLeave={() => setOver(false)}
         onDrop={(e) => {
           e.preventDefault()
           setOver(false)
           take(e.dataTransfer.files)
         }}>
      <input ref={input} type="file" accept=".docx" className="sr-only"
             onChange={(e) => {
               take(e.target.files)
               e.target.value = '' // picking the same file twice still fires
             }} />
      {busy ? (
        <span className="drop__busy"><Spinner />Uploading…</span>
      ) : (
        <>
          <Icon d={P.upload} size={18} />
          <span>
            Drop a <b>.docx</b> here, or{' '}
            <button type="button" className="linklike" onClick={() => input.current?.click()}>choose a file</button>
          </span>
          <span className="help">Only .docx. A failed upload leaves your current master untouched.</span>
        </>
      )}
    </div>
  )
}

/** One tailored version: a dense row that opens to its summary, bullets and
 * the facts each bullet cites. */
function VersionRow({ v, open, onToggle }: { v: Version; open: boolean; onToggle: () => void }) {
  const job = `${v.company} · ${v.title}`
  return (
    <>
      <div className={open ? 'vrow vrow--open' : 'vrow'}>
        <button type="button" className="vrow__toggle" aria-expanded={open} onClick={onToggle}
                aria-label={`${open ? 'Hide' : 'Show'} ${job}`}>
          <Icon d={open ? P.chevronDown : P.chevronRight} />
        </button>
        {v.job_id ? <Link className="vrow__job" to={`/jobs/${v.job_id}`}>{job}</Link>
                  : <span className="vrow__job strong">{job}</span>}
        <span className="mono vrow__version">{v.version}</span>
        <span className="mono faint">{day(v.created_at)}</span>
        <span className="mono">{v.bullets.length}</span>
        <span className="mono faint">{v.prompt_version ?? '—'}</span>
        <a className="vrow__download" href={`/resume/${v.version}`} download>Download</a>
      </div>
      {open && (
        <div className="vdetail">
          {v.summary ? <p className="vdetail__summary">{v.summary}</p>
                     : <p className="vdetail__summary faint">No summary recorded for this version.</p>}
          <div className="vdetail__bullets">
            {v.bullets.length === 0 && <span className="help">No bullets recorded for this version.</span>}
            {v.bullets.map((b, i) => (
              <div className="vbullet" key={i}>
                <span className="vbullet__text">• {b.text}</span>
                {b.fact_ids.length > 0 && (
                  <span className="vbullet__facts">
                    {b.fact_ids.map((id, j) => {
                      const claim = b.fact_claims[j]
                      // resumes_context resolves a citation whose fact is gone
                      // to "unknown fact" -- say so rather than link to nothing.
                      return claim && claim !== 'unknown fact' ? (
                        <Link className="fact" key={id} to={`/facts#fact-${id}`}>
                          <span className="mono">#{id}</span>{claim}
                        </Link>
                      ) : (
                        <span className="fact fact--gone" key={id}>
                          <span className="mono">#{id}</span>Unknown fact
                        </span>
                      )
                    })}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}

/** Resumes (approved Screen 8): the master template tailoring renders into,
 * and every tailored version. One version per job, created on its first Apply
 * and reused by Redo draft and Continue -- so there is no re-tailor action. */
export function Resumes() {
  const { toast } = useShell()
  const [data, setData] = useState<ResumesContext | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [upload, setUpload] = useState<UploadResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [replacing, setReplacing] = useState(false)
  const [howOpen, setHowOpen] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const load = useCallback(
    () =>
      get<ResumesContext>('/api/resumes')
        .then((d) => {
          setData(d)
          setLoadError(null)
        })
        .catch((e: unknown) => setLoadError(errorText(e))),
    [],
  )

  useEffect(() => {
    load()
  }, [load])

  async function send(file: File) {
    setBusy(true)
    setUpload(null)
    try {
      const res = await uploadFile<UploadResult>('/api/resumes', file)
      setUpload(res)
      setReplacing(false)
      await load()
      toast({ text: 'Master resume installed' })
    } catch (err) {
      // A refusal (422) leaves the installed master untouched.
      const body = err instanceof ApiError ? (err.body as UploadResult | undefined) : undefined
      setUpload({ ok: false, message: body?.message ?? errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  if (!data) {
    return (
      <div className="page resumes">
        <div className="page-head"><h1>Resumes</h1></div>
        {loadError ? (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={16} />
            <span><b>Can't load your resumes:</b> {loadError}</span>
            <button type="button" className="btn resumes__retry" onClick={load}>Retry</button>
          </div>
        ) : (
          <div aria-busy="true" className="resumes__skel">
            {[0, 1].map((i) => (
              <div className="card resumes__skel-card" key={i}>
                {[30, 80, 60].map((w) => <span className="skel" key={w} style={{ width: `${w}%` }} />)}
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  const { master, versions } = data
  const q = filter.trim().toLowerCase()
  const shown = q ? versions.filter((v) => `${v.company} ${v.title}`.toLowerCase().includes(q)) : versions

  return (
    <div className="page resumes">
      <TopBarActions>
        <button type="button" className={master.exists ? 'btn' : 'btn pri'} disabled={busy}
                onClick={() => setReplacing((r) => !r)}>
          <Icon d={P.upload} />
          {master.exists ? 'Replace master' : 'Upload master'}
        </button>
      </TopBarActions>

      <div className="page-head">
        <h1>Resumes</h1>
        <p>The master template every tailored resume is rendered from.</p>
      </div>

      <section className="card">
        <div className="master">
          <span className={master.exists ? 'master__icon' : 'master__icon master__icon--none'}>
            <Icon d={P.fileLines} size={22} width={1.75} />
          </span>
          <div className="master__text">
            {master.exists ? (
              <>
                <span className="master__title">
                  <span className="ct">Master resume</span>
                  <span className="b em">Ready for tailoring</span>
                </span>
                <span className="master__meta">
                  <span className="mono master__path">{master.path}</span>
                  {master.modified && <> · modified <span className="mono">{minute(master.modified)}</span></>}
                </span>
              </>
            ) : (
              <>
                <span className="ct">No master resume</span>
                <span className="master__meta">
                  Tailoring and Apply can't run until you upload one — <span className="mono">{master.path}</span>
                </span>
              </>
            )}
          </div>
        </div>

        {(replacing || !master.exists) && (
          <div className="master__upload"><Dropzone busy={busy} onFile={send} /></div>
        )}

        {upload && (
          <div className={upload.ok ? 'master__result master__result--ok' : 'alert master__result'}
               role={upload.ok ? 'status' : 'alert'}>
            <Icon d={upload.ok ? P.check : P.refused} size={16} width={upload.ok ? 2.5 : 2} />
            <div className="master__result-text">
              <b>{upload.message}</b>
              {upload.ok && upload.changes && upload.changes.length > 0 && (
                <>
                  <span className="master__changes-head">What we changed in your file</span>
                  <ul className="master__changes">
                    {upload.changes.map((c) => <li className="mono" key={c}>{c}</li>)}
                  </ul>
                </>
              )}
              {upload.ok && (
                <span className="master__note">
                  Replacing the master affects future tailoring only — existing versions keep the file
                  they were rendered from.
                </span>
              )}
            </div>
          </div>
        )}

        <button type="button" className="master__how" aria-expanded={howOpen} onClick={() => setHowOpen((h) => !h)}>
          <Icon d={howOpen ? P.chevronDown : P.chevronRight} />
          How the template works
        </button>
        {howOpen && (
          <div className="master__how-body">
            <p>Tailoring fills two marker paragraphs and leaves everything else in the document exactly as it is:</p>
            <ul>
              <li><span className="mono">{MARKERS[0]}</span> becomes the tailored summary.</li>
              <li><span className="mono">{MARKERS[1]}</span> is cloned once per tailored bullet.</li>
            </ul>
            <p className="faint">
              A file without the markers is prepared automatically on upload, and the result above lists exactly
              what was replaced. A file whose structure can't be read is refused rather than guessed at.
            </p>
          </div>
        )}
      </section>

      <section className="card">
        <div className="ch">
          <span className="versions__head">
            <h2 className="ct">Tailored versions</h2>
            <span className="mono faint">{versions.length}</span>
            <span className="help">· one per job, reused on Redo draft and Continue</span>
          </span>
          {versions.length > 0 && (
            <span className="versions__filter">
              <Icon d={P.search} />
              <input className="field" value={filter} onChange={(e) => setFilter(e.target.value)}
                     placeholder="Filter by company or role" aria-label="Filter versions" />
            </span>
          )}
        </div>

        {versions.length === 0 ? (
          <div className="empty">
            <span className="empty__icon"><Icon d={P.fileLines} size={16} /></span>
            No tailored resumes yet
            <span className="help">One is created the first time you Apply to a job.</span>
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
            {shown.length === 0 && (
              <div className="empty">
                No versions match
                <button type="button" className="btn" onClick={() => setFilter('')}>Clear filter</button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  )
}
