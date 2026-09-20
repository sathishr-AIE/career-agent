import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorText, get, post } from '../api'
import { useShell } from './shell'
import { useAction } from './useAction'

// ponytail: an Apply-style request lasts the whole agent run, so the page
// waits this long for a fast guard refusal before opening the job's chat. A
// refusal after that arrives as a toast (the shell's toasts survive navigation).
const REFUSAL_WAIT_MS = 1000

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** One job's actions, with its busy/refusal state. A refusal shows inline
 * (`error`) and as a toast. Used by Applications rows and the job hub. */
export function useJobActions(jobId: number, reload: () => void) {
  const { toast } = useShell()
  const nav = useNavigate()
  const { busy, error, run } = useAction(reload, (m) => toast({ text: m, tone: 'error' }))
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  /** Interim target: the job's chat page. Screen 6 moves it to the hub's Chat tab. */
  const openChat = () =>
    get<{ id: number }>(`/api/chat/jobs/${jobId}/conversation`)
      .then((c) => nav(`/chat/${c.id}`))
      .catch((e: unknown) => toast({ text: errorText(e), tone: 'error' }))

  const act = (path: string, body?: unknown, done?: string) =>
    run(path, body).then((r) => {
      if (r !== undefined && done) toast({ text: done })
      return r
    })

  /** Apply, Redo draft, Track anyway: start the agent, then open its chat. */
  const startRun = async (path: string) => {
    setStarting(true)
    setStartError(null)
    const req = post(path)
    const early = await Promise.race([
      req.then(() => 'ok' as const, (e: unknown) => e),
      wait(REFUSAL_WAIT_MS).then(() => 'slow' as const),
    ])
    setStarting(false)
    if (early !== 'ok' && early !== 'slow') {
      const m = errorText(early)
      setStartError(m)
      toast({ text: m, tone: 'error' })
      return
    }
    req.catch((e: unknown) => toast({ text: errorText(e), tone: 'error' }))
    openChat()
  }

  /** Continue an interrupted session, then open its chat. */
  const resume = () =>
    act(`/api/chat/jobs/${jobId}/resume`).then((r) => {
      if (r !== undefined) openChat()
    })

  return { busy: busy || starting, error: startError ?? error, act, startRun, resume, openChat }
}

export type JobActions = ReturnType<typeof useJobActions>
