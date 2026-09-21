// Every backend timestamp is SQLite's naive-UTC datetime('now') text
// ("2026-09-18 09:42:00"). Parse it as UTC, show it in local time.

export function parseUtc(utc: string): Date {
  return new Date(`${utc.replace(' ', 'T')}Z`)
}

/** Local "HH:MM". */
export function clock(utc: string): string {
  return parseUtc(utc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** "HH:MM" today, a short date ("12 Sep") otherwise. */
export function shortTime(utc: string): string {
  const d = parseUtc(utc)
  return d.toDateString() === new Date().toDateString()
    ? clock(utc)
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Compact age: "now", "4m", "3h", "2d". */
export function ago(utc: string): string {
  const s = (Date.now() - parseUtc(utc).getTime()) / 1000
  if (s < 60) return 'now'
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

const pad = (n: number) => String(n).padStart(2, '0')

/** Local "YYYY-MM-DD HH:MM". */
export function dateTime(utc: string): string {
  const d = parseUtc(utc)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(utc)}`
}

/** Local "HH:MM" today, "MM-DD HH:MM" otherwise (timeline rows). */
export function stamp(utc: string): string {
  const d = parseUtc(utc)
  return d.toDateString() === new Date().toDateString()
    ? clock(utc)
    : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(utc)}`
}

/** Transcript day divider: "Today", "Yesterday", or "12 Sep". */
export function dayLabel(utc: string): string {
  const d = parseUtc(utc)
  const midnight = new Date()
  midnight.setHours(0, 0, 0, 0)
  const days = Math.floor((midnight.getTime() - d.getTime()) / 86400000) + 1
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Local calendar date, "2026-09-14". */
export function localDay(utc: string): string {
  const d = parseUtc(utc)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
