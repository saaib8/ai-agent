import type { NormalisedDimensions } from '../api/types'

/** Format a Decimal-as-string amount with its unit, e.g. "2,950 SAR".
 *  Falls back to the raw string if it is not a finite number. */
export function money(amount: string | null | undefined, unit: string | null | undefined): string | null {
  if (amount == null) return null
  const n = Number(amount)
  const shown = Number.isFinite(n)
    ? n.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : String(amount)
  return unit ? `${shown} ${unit}` : shown
}

/** A number for arithmetic (budget bars), or null when not parseable. */
export function toNumber(value: string | null | undefined): number | null {
  if (value == null) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function cm(value: string | null): string | null {
  if (value == null) return null
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 1 }) : String(value)
}

/** "L 220 · W 95 · H 85 cm", or null when the catalog could not normalise. */
export function dimensionsLine(d: NormalisedDimensions | null | undefined): string | null {
  if (!d || d.status !== 'normalised') return null
  const parts: string[] = []
  const l = cm(d.length_cm)
  const w = cm(d.width_cm)
  const h = cm(d.height_cm)
  if (l) parts.push(`L ${l}`)
  if (w) parts.push(`W ${w}`)
  if (h) parts.push(`H ${h}`)
  return parts.length ? `${parts.join(' · ')} cm` : null
}

/** Registry keys and enum values → customer-facing words: "lounge-chair" → "lounge chair". */
export function humanise(value: string | null | undefined): string {
  if (!value) return ''
  return value.replace(/[-_]/g, ' ')
}
