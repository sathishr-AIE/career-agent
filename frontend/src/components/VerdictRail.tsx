import type { Verdict } from '../api'
import './VerdictRail.css'

/** hue = verdict, fill height = fraction (0-1). `size="row"` for a job-list
 * leading edge; `size="wide"` for the dashboard's score-distribution bars. */
export function VerdictRail({
  verdict,
  fraction,
  size = 'row',
}: {
  verdict: Verdict | null
  fraction: number
  size?: 'row' | 'wide'
}) {
  const cls = verdict ?? 'none'
  const pct = Math.max(0, Math.min(1, fraction)) * 100
  return (
    <div
      className={`verdict-rail verdict-rail--${size} verdict-rail--${cls}`}
      role="img"
      aria-label={verdict ? `${verdict}, ${Math.round(pct)} score` : 'not yet scored'}
    >
      <div className="verdict-rail__fill" style={{ height: `${pct}%` }} />
    </div>
  )
}
