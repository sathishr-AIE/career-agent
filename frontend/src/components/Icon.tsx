/** A 24x24 stroke icon (fill for solid shapes). Paths live in src/icons.ts. */
export function Icon({ d, size = 14, width = 2, fill = false }: {
  d: string
  size?: number
  width?: number
  fill?: boolean
}) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill ? 'currentColor' : 'none'}
         stroke={fill ? 'none' : 'currentColor'} strokeWidth={width} strokeLinecap="round"
         strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

/** The component sheet's busy ring, in the current text colour. */
export function Spinner() {
  return (
    <svg className="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity={0.35} strokeWidth={3} />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth={3} strokeLinecap="round" />
    </svg>
  )
}
