import { useCallback, useEffect, useState } from 'react'
import { ApiError, errorText, get } from '../api'

interface Result<T> {
  path: string
  data: T | null
  error: string | null
  status: number | null
}

/** GET `path` now and every `ms` (`ms` 0: once, until `reload`). A failed
 * poll keeps the last good data; `error` says why and `status` is its HTTP
 * status (e.g. 404). `reload` re-fetches at once and restarts the interval.
 * A tick is skipped while the previous request is still out, so a slow
 * endpoint never stacks requests up. Results are keyed by path: after the
 * path changes (another job, say), the old path's data is never shown. */
export function usePoll<T>(path: string, ms = 3000) {
  const [result, setResult] = useState<Result<T> | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    let inFlight = false
    const tick = () => {
      if (inFlight) return
      inFlight = true
      get<T>(path)
        .then((d) => {
          if (live) setResult({ path, data: d, error: null, status: null })
        })
        .catch((e: unknown) => {
          if (!live) return
          setResult((r) => ({
            path,
            data: r?.path === path ? r.data : null,
            error: errorText(e),
            status: e instanceof ApiError ? e.status : null,
          }))
        })
        .finally(() => {
          inFlight = false
        })
    }
    tick()
    const id = ms > 0 ? setInterval(tick, ms) : undefined
    return () => {
      live = false
      clearInterval(id)
    }
  }, [path, ms, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const mine = result?.path === path ? result : null
  return { data: mine?.data ?? null, error: mine?.error ?? null, status: mine?.status ?? null, reload }
}
