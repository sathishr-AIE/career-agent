import { useCallback, useEffect, useState } from 'react'
import { get, type ChatMessage, type OpenPrompt } from '../../api'

const POLL_MS = 3000
/** Consecutive failed polls before the transcript says it's offline. */
const OFFLINE_AFTER = 3

interface Page {
  messages: ChatMessage[]
  open_prompt: OpenPrompt | null
  resumable: boolean
}

interface Feed extends Omit<Page, 'messages' | 'open_prompt'> {
  cid: number
  messages: ChatMessage[]
  openPrompt: OpenPrompt | null
}

/** One conversation's live transcript: the first GET returns the newest
 * window, every later one appends past an exclusive cursor. The feed is
 * keyed by conversation, so switching conversations shows nothing from the
 * old one without clearing state in an effect. Polling Home's messages is
 * also what expires a stale Home card, so the interval keeps running even
 * with no card on screen. */
export function useTranscript(cid: number | null) {
  const [feed, setFeed] = useState<Feed | null>(null)
  const [offline, setOffline] = useState(false)
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    if (cid === null) return
    let live = true
    let inFlight = false
    let cursor = 0
    let failures = 0

    const tick = () => {
      if (inFlight) return          // a slow poll must not stack, or re-read a stale cursor
      inFlight = true
      get<Page>(`/api/chat/${cid}/messages?after=${cursor}`)
        .then((page) => {
          if (!live) return
          failures = 0
          setOffline(false)
          cursor = page.messages.reduce((top, m) => Math.max(top, m.id), cursor)
          setFeed((prev) => {
            const kept = prev?.cid === cid ? prev.messages : []
            const seen = new Set(kept.map((m) => m.id))
            return {
              cid,
              messages: kept.concat(page.messages.filter((m) => !seen.has(m.id))),
              openPrompt: page.open_prompt,
              resumable: page.resumable,
            }
          })
        })
        .catch(() => {
          failures += 1
          if (live && failures >= OFFLINE_AFTER) setOffline(true)
        })
        .finally(() => {
          inFlight = false
        })
    }

    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [cid, nonce])

  const mine = feed?.cid === cid ? feed : null
  return {
    /** null until the first poll lands: the page shows its skeleton. */
    messages: mine?.messages ?? null,
    openPrompt: mine?.openPrompt ?? null,
    resumable: mine?.resumable ?? false,
    offline,
    reload,
  }
}
