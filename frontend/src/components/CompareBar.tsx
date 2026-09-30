import { useState } from 'react'
import type { CheckedCard } from '../lib/compare'
import { CloseIcon, CompareIcon } from './icons'

interface CompareBarProps {
  /** The cards checked for comparison: any number, all of one family. */
  checked: CheckedCard[]
  onCompare: () => void
  onUncheck: (card: CheckedCard) => void
  onClear: () => void
}

/**
 * The cards checked to compare, above the message box.
 *
 * Checking a card only marks it; the comparison runs when they press Compare,
 * once at least two similar products are checked - as many as they like, up to
 * the server's limit. Any can be unchecked here as well as on its card.
 */
export function CompareBar({ checked, onCompare, onUncheck, onClear }: CompareBarProps) {
  if (checked.length === 0) return null
  const ready = checked.length >= 2

  return (
    <div className="border-t border-line bg-canvas/70 px-4 py-2.5">
      <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2 sm:flex-nowrap">
        <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto [scrollbar-width:thin]">
          <span className="hidden shrink-0 text-[11px] font-semibold uppercase tracking-wide text-muted sm:inline">
            Compare
          </span>
          {checked.map((card) => (
            <CheckedTile
              key={`${card.listRevision}:${card.ordinal}`}
              card={card}
              onUncheck={() => onUncheck(card)}
            />
          ))}
          {!ready && (
            <div className="flex h-11 min-w-40 flex-1 items-center rounded-xl border border-dashed border-line-strong px-3 text-xs text-muted">
              <span className="truncate">Check one more of the same kind</span>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onClear}
            className="rounded-full px-3 py-2 text-xs font-medium text-muted transition hover:bg-surface hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={onCompare}
            disabled={!ready}
            title={ready ? undefined : 'Check at least two products of the same kind'}
            className="inline-flex items-center gap-1.5 rounded-full bg-clay px-4 py-2 text-sm font-semibold text-white shadow-card transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:bg-line-strong disabled:text-muted disabled:shadow-none"
          >
            <CompareIcon size={15} />
            Compare{ready ? ` ${checked.length}` : ''}
          </button>
        </div>
      </div>
    </div>
  )
}

function CheckedTile({ card, onUncheck }: { card: CheckedCard; onUncheck: () => void }) {
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
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink" title={card.name}>
        {card.name}
      </span>
      <button
        type="button"
        onClick={onUncheck}
        aria-label={`Stop comparing ${card.name}`}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-rose"
      >
        <CloseIcon size={13} />
      </button>
    </div>
  )
}
