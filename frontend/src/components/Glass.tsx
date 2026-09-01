import type { ReactNode } from 'react'
import './Glass.css'

/** A translucent chrome surface (sidebar, status bar) over the graphite
 * shell. Not for content surfaces -- those live on the paper plane. */
export function Glass({
  children,
  className = '',
  as: Tag = 'div',
}: {
  children: ReactNode
  className?: string
  as?: 'div' | 'nav' | 'footer'
}) {
  return <Tag className={`glass ${className}`}>{children}</Tag>
}
