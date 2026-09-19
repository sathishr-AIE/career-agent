import { useCallback, useEffect, useState } from 'react'
import { errorText, get } from '../api'

/** GET `path` now and every `ms`. A failed poll keeps the last good data
 * (`error` says why); `reload` re-fetches at once and restarts the interval. */
export function usePoll<T>(path: string, ms = 3000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    const tick = () => {
      get<T>(path)
        .then((d) => {
          if (!live) return
          setData(d)
          setError(null)
        })
        .catch((e: unknown) => {
          if (live) setError(errorText(e))
        })
    }
    tick()
    const id = setInterval(tick, ms)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [path, ms, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { data, error, reload }
}
