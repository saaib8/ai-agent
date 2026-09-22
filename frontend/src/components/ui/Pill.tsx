import type { ReactNode } from 'react'

type PillTone = 'neutral' | 'accent' | 'success' | 'warn' | 'danger'

const TONES: Record<PillTone, string> = {
  neutral: 'border-line text-muted',
  accent: 'border-clay/30 text-clay',
  success: 'border-sage/40 text-sage',
  warn: 'border-amber/40 text-amber',
  danger: 'border-rose/40 text-rose',
}

interface PillProps {
  children: ReactNode
  tone?: PillTone
  className?: string
}

/** A small outlined status/statistic pill — the recurring NORA affordance. */
export function Pill({ children, tone = 'neutral', className = '' }: PillProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border bg-surface/60 px-2.5 py-0.5 text-xs font-medium ${TONES[tone]} ${className}`}
    >
      {children}
    </span>
  )
}

/** A coloured status dot, sized to sit inside a Pill. */
export function Dot({ tone }: { tone: PillTone }) {
  const color: Record<PillTone, string> = {
    neutral: 'bg-muted',
    accent: 'bg-clay',
    success: 'bg-sage',
    warn: 'bg-amber',
    danger: 'bg-rose',
  }
  return <span className={`h-1.5 w-1.5 rounded-full ${color[tone]}`} />
}
