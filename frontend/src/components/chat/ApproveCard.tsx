import { Icon } from '../Icon'
import { P } from '../../icons'
import { useAnswer } from './useAnswer'
import type { OpenPrompt } from '../../api'

/** What each Home action actually spends, under the backend's own question
 * (actions._ask_home). Screen 6's job cards have their own copy in the
 * payload, so this map stays Home-only. */
const WHAT: Record<string, string> = {
  find_jobs: 'Searches LinkedIn, Naukri and your Greenhouse boards, then scores the new matches.',
  apply_to: 'Tailors your resume and drives the application in a browser.',
}

/** A Home `approve` card: the only card that spends credits on a go-ahead.
 * A refusal (the 10-minute expiry, or a newer card superseding this one)
 * shows inside the card and leaves it open, because the backend refused --
 * nothing was started. */
export function ApproveCard({ prompt, onAnswered }: {
  prompt: OpenPrompt
  onAnswered: () => void
}) {
  const { busy, error, send } = useAnswer(prompt.id, onAnswered)
  const payload = prompt.payload as { question?: string; action?: string }
  const what = WHAT[payload.action ?? '']

  return (
    <section className="ccard card">
      <div className="ccard__head ccard__head--go">
        <Icon d={P.shieldCheck} size={14} />
        Needs your go-ahead
      </div>
      <div className="ccard__body">
        <p className="ccard__q">{payload.question ?? 'Go ahead?'}</p>
        {what && <p className="ccard__sub">{what}</p>}
        {error && (
          <div className="alert" role="alert">
            <Icon d={P.refused} size={14} />
            <span><b>Refused:</b> {error}</span>
          </div>
        )}
        <div className="ccard__actions">
          <button className="btn pri" disabled={busy}
                  onClick={() => send({ answer: 'approve' })}>Approve</button>
          <button className="btn" disabled={busy}
                  onClick={() => send({ answer: 'reject' })}>Not now</button>
          <span className="ccard__hint">A newer request replaces this one.</span>
        </div>
      </div>
    </section>
  )
}
