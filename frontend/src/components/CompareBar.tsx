import { useState } from 'react'
import type { CardLabels } from '../api/types'
import type { CheckedCard } from '../lib/compare'
import { cardWords } from '../lib/cardWords'
import { CloseIcon, CompareIcon } from './icons'

interface CompareBarProps {
  /** The cards checked for comparison: any number, all of one family. */
  checked: CheckedCard[]
  busy: boolean
  onCompare: () => void
  onUncheck: (card: CheckedCard) => void
  onClear: () => void
  /** The conversation's latest words (`ChatResponse.labels`). */
  labels?: CardLabels | null
}

/**
 * The cards checked to compare, above the message box.
 *
 * Checking a card only marks it; the comparison runs when they press Compare,
 * once at least two similar products are checked - as many as they like, up to
 * the server's limit. Any can be unchecked here as well as on its card.
 */
export function CompareBar({
  checked,
  busy,
  onCompare,
  onUncheck,
  onClear,
  labels,
}: CompareBarProps) {
  if (checked.length === 0) return null
  const ready = checked.length >= 2
  const words = cardWords(labels).compareBar

  return (
    <div className="border-t border-line bg-canvas/70 px-4 py-2.5">
      <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2 sm:flex-nowrap">
        <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto [scrollbar-width:thin]">
          <span className="hidden shrink-0 text-[11px] font-semibold uppercase tracking-wide text-muted sm:inline">
            {words.title}
          </span>
          {checked.map((card) => (
            <CheckedTile
              key={`${card.listRevision}:${card.ordinal}`}
              card={card}
              stopLabel={words.stopName(card.name)}
              onUncheck={() => onUncheck(card)}
            />
          ))}
          {!ready && (
            <div className="flex h-11 min-w-40 flex-1 items-center rounded-xl border border-dashed border-line-strong px-3 text-xs text-muted">
              <span className="truncate">{words.oneMore}</span>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onClear}
            className="rounded-full px-3 py-2 text-xs font-medium text-muted transition hover:bg-surface hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30"
          >
            {words.clear}
          </button>
          <button
            type="button"
            onClick={onCompare}
            disabled={!ready || busy}
            title={ready ? undefined : words.needTwo}
            className="inline-flex items-center gap-1.5 rounded-full bg-clay px-4 py-2 text-sm font-semibold text-white shadow-card transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:bg-line-strong disabled:text-muted disabled:shadow-none"
          >
            <CompareIcon size={15} />
            {words.compare(ready ? checked.length : null)}
          </button>
        </div>
      </div>
    </div>
  )
}

function CheckedTile({
  card,
  stopLabel,
  onUncheck,
}: {
  card: CheckedCard
  stopLabel: string
  onUncheck: () => void
}) {
  const [imgFailed, setImgFailed] = useState(false)
  return (
    <div className="flex h-11 w-44 shrink-0 items-center gap-2 rounded-xl border border-line bg-surface p-1.5 pr-1 shadow-card sm:w-52">
      <div className="h-8 w-8 shrink-0 overflow-hidden rounded-lg bg-canvas">
        {!imgFailed && card.imageUrl ? (
          <img
            src={card.imageUrl}
            alt=""
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink" title={card.name} dir="auto">
        {card.name}
      </span>
      <button
        type="button"
        onClick={onUncheck}
        aria-label={stopLabel}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-rose"
      >
        <CloseIcon size={13} />
      </button>
    </div>
  )
}
