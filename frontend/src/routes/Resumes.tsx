import { useEffect, useRef, useState } from 'react'
import { ApiError, get, uploadFile, type ActionResult } from '../api'

interface Bullet {
  text: string
  fact_ids: number[]
  fact_claims: string[]
}

interface ResumeVersion {
  version: string
  created_at: string
  company: string
  title: string
  summary: string
  bullets: Bullet[]
}

interface ResumesContext {
  master: { exists: boolean; path: string; modified?: string }
  versions: ResumeVersion[]
}

export function Resumes() {
  const [data, setData] = useState<ResumesContext | null>(null)
  const [result, setResult] = useState<ActionResult | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = () => get<ResumesContext>('/api/resumes').then(setData)

  useEffect(() => {
    reload()
  }, [])

  async function onUpload(e: React.FormEvent) {
    e.preventDefault()
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setUploading(true)
    setResult(null)
    try {
      const res = await uploadFile<ActionResult>('/api/resumes', file)
      setResult(res)
      if (res.ok) {
        if (fileRef.current) fileRef.current.value = ''
        reload()
      }
    } catch (err) {
      setResult({ ok: false, message: err instanceof ApiError ? err.message : 'Upload failed.' })
    } finally {
      setUploading(false)
    }
  }

  if (!data) return null
  const { master, versions } = data

  return (
    <>
      <h1 className="page-title">Resumes</h1>

      <div className="card">
        <h2>Primary (master) resume</h2>
        {master.exists ? (
          <div className="kv">
            <div>
              <span>File</span>
              <b>{master.path}</b>
            </div>
            <div>
              <span>Last modified</span>
              <b>{master.modified}</b>
            </div>
          </div>
        ) : (
          <p className="rationale">
            No master template found at {master.path}. Upload your resume below before
            tailoring can run.
          </p>
        )}

        <form onSubmit={onUpload}>
          <input ref={fileRef} type="file" accept=".docx" required />
          <button className="btn" type="submit" disabled={uploading}>
            {master.exists ? 'Replace master' : 'Upload master'}
          </button>
        </form>
        {result && (
          <div className={`banner banner--${result.ok ? 'done' : 'denied'}`}>
            {result.message}
            {result.changes && result.changes.length > 0 && (
              <ul>
                {result.changes.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <p className="rationale">
          Must be a .docx containing two marker paragraphs, each on its own line:{' '}
          <b>&lt;&lt;SUMMARY&gt;&gt;</b> where the tailored summary goes, and{' '}
          <b>&lt;&lt;PROJECT_BULLET&gt;&gt;</b> where bullets go (it is cloned once per
          generated bullet). Everything else — header, contact details, skills, education —
          is left untouched.
        </p>
      </div>

      <h2 className="page-title" style={{ fontSize: '1.25rem' }}>
        Tailored versions
      </h2>
      {versions.length === 0 ? (
        <p className="rationale">
          No tailored resumes generated yet — the first one is created the first time you
          click Apply on a job.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Version</th>
              <th>Generated</th>
              <th>Bullets</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td>
                  {v.title} at {v.company}
                </td>
                <td className="data">{v.version}</td>
                <td className="data">{v.created_at}</td>
                <td>
                  {v.summary && (
                    <div className="rationale">
                      <em>{v.summary}</em>
                    </div>
                  )}
                  {v.bullets.map((b, i) => (
                    <div
                      key={i}
                      className="rationale"
                      title={b.fact_claims.join('; ')}
                    >
                      {b.text}{' '}
                      <i>
                        (fact {b.fact_ids.join(', ')}: {b.fact_claims.join('; ')})
                      </i>
                    </div>
                  ))}
                </td>
                <td>
                  <a href={`/resume/${v.version}`} target="_blank" rel="noreferrer">
                    Download
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
