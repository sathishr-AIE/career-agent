import { useState } from 'react'
import { answerPrompt, errorText } from '../../api'

/** One POST per card. `busy` stays true after a success so the card can't be
 * answered twice before the next poll removes it; only an error re-enables. */
export function useAnswer(promptId: number, onAnswered: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const send = (body: Record<string, unknown>) => {
    if (busy) return
    setBusy(true)
    setError(null)
    answerPrompt(promptId, body)
      .then(() => onAnswered())
      .catch((e) => {
        setError(errorText(e))
        setBusy(false)
      })
  }
  return { busy, error, send }
}
