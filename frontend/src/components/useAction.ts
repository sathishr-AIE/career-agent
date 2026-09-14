import { useState } from 'react'
import { errorText, post } from '../api'

/** One POST, with the backend's refusal surfaced instead of swallowed.
 * Unlike chat's useAnswer (which stays busy until the next poll removes the
 * card), an action button here is clickable again right after it settles --
 * Start/Pause/Stop and the queue's ▲/▼/Skip are all meant to be pressed
 * again. `error` is left in place after a success (cleared at the next
 * run) so a stale message never lingers past its cause. */
export function useAction(onChanged: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function run<T = unknown>(path: string, body?: unknown): Promise<T | undefined> {
    setBusy(true)
    setError(null)
    return post<T>(path, body)
      .then((r) => {
        onChanged()
        return r
      })
      .catch((e: unknown) => {
        setError(errorText(e))
        return undefined
      })
      .finally(() => setBusy(false))
  }

  return { busy, error, run }
}
