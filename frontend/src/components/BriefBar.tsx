import type { BriefChip, CardLabels } from '../api/types'
import { cardWords } from '../lib/cardWords'
import { CloseIcon } from './icons'

interface BriefBarProps {
  chips: BriefChip[]
  /** The reply's words (`ChatResponse.labels`) - English without them. */
  labels?: CardLabels | null
  busy: boolean
  onDrop: (chip: BriefChip) => void
}

/** What the search on screen is using, each taken away by its ✕. */
export function BriefBar({ chips, labels, busy, onDrop }: BriefBarProps) {
  const words = cardWords(labels).brief
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map((chip) => (
        <span
          key={chip.facet}
          className="inline-flex items-center gap-1 rounded-full border border-clay/30 bg-clay-soft/40 py-0.5 pe-1 ps-3 text-[13px] font-medium text-ink"
        >
          {chip.label}
          <button
            onClick={() => onDrop(chip)}
            disabled={busy}
            aria-label={words.remove(chip.label)}
            className="rounded-full p-1 text-muted transition hover:bg-clay hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            <CloseIcon size={12} />
          </button>
        </span>
      ))}
    </div>
  )
}
