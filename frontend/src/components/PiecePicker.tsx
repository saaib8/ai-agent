import { useState } from 'react'
import type { PiecePickerData } from '../api/types'

interface PiecePickerProps {
  picker: PiecePickerData
  onSend: (text: string) => void
  disabled: boolean
}

/** A room's pieces as chips to tick and untick, sent together as one message -
 *  the same path a typed answer takes. The usual pieces start ticked. */
export function PiecePicker({ picker, onSend, disabled }: PiecePickerProps) {
  const [ticked, setTicked] = useState<Set<string>>(
    () => new Set(picker.pieces.filter((p) => p.selected).map((p) => p.label)),
  )

  const toggle = (label: string) =>
    setTicked((current) => {
      const next = new Set(current)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })

  const chosen = picker.pieces.filter((p) => ticked.has(p.label)).map((p) => p.label)

  return (
    <div className="flex animate-rise flex-col gap-2.5 pt-0.5">
      <div className="flex flex-wrap gap-2">
        {picker.pieces.map((piece) => {
          const on = ticked.has(piece.label)
          return (
            <button
              key={piece.label}
              onClick={() => toggle(piece.label)}
              disabled={disabled}
              aria-pressed={on}
              className={
                'rounded-full border px-3.5 py-1.5 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50 ' +
                (on
                  ? 'border-clay bg-clay text-white'
                  : 'border-line bg-surface text-muted hover:border-clay/50 hover:text-ink')
              }
            >
              {on ? '✓ ' : '+ '}
              {piece.label}
            </button>
          )
        })}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => onSend(`I'd like these pieces: ${chosen.join(', ')}`)}
          disabled={disabled || chosen.length === 0}
          className="rounded-full bg-ink px-4 py-1.5 text-sm font-medium text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {picker.submit_label}
        </button>
        <button
          onClick={() => onSend(picker.choose_for_me.value)}
          disabled={disabled}
          className="rounded-full border border-clay/30 bg-surface px-3.5 py-1.5 text-sm font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {picker.choose_for_me.label}
        </button>
      </div>
    </div>
  )
}
