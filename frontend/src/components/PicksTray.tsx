import { useState } from 'react'
import type { PickView } from '../api/types'
import { money } from '../lib/format'
import { CloseIcon, CompareIcon, SparkIcon } from './icons'

interface PicksTrayProps {
  picks: PickView[]
  /** A reply or a tick is in flight: actions wait for it. */
  busy: boolean
  /** Why the last tick was refused, in the server's words. */
  error: string | null
  onRemove: (pick: PickView) => void
  onGoesWith: (pick: PickView) => void
  onCompare: (first: PickView, second: PickView) => void
}

/**
 * The customer's picks, kept across searches, above the message box.
 *
 * Drawn from what the server reports, never from local guesses, so a typed
 * "I'll take the second one" and a tick land in the same tray. Any pick can
 * show what goes with it; two can be compared - with more than two, the
 * customer marks which two.
 */
export function PicksTray({
  picks,
  busy,
  error,
  onRemove,
  onGoesWith,
  onCompare,
}: PicksTrayProps) {
  const [marked, setMarked] = useState<number[]>([])
  if (picks.length === 0) return error ? <TrayError message={error} /> : null

  // Marks follow the picks: one that was removed is no longer marked.
  const live = marked.filter((n) => picks.some((p) => p.pick === n))
  const pair: PickView[] =
    picks.length === 2
      ? picks
      : live.map((n) => picks.find((p) => p.pick === n)).filter((p): p is PickView => !!p)
  const canCompare = pair.length === 2 && !busy

  const toggleMark = (n: number) =>
    setMarked((prev) => {
      const current = prev.filter((m) => picks.some((p) => p.pick === m))
      if (current.includes(n)) return current.filter((m) => m !== n)
      // Two at a time: marking a third replaces the oldest mark.
      return [...current, n].slice(-2)
    })

  return (
    <div className="border-t border-line bg-canvas/70 px-4 pt-2.5">
      <div className="mx-auto max-w-3xl">
        <div className="mb-2 flex items-center justify-between gap-3">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
            Your picks ({picks.length})
          </span>
          {picks.length >= 2 && (
            <button
              onClick={() => canCompare && onCompare(pair[0], pair[1])}
              disabled={!canCompare}
              title={
                picks.length > 2 && pair.length < 2 ? 'Mark two picks to compare' : undefined
              }
              className="inline-flex items-center gap-1.5 rounded-full bg-clay px-3.5 py-1.5 text-xs font-semibold text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:bg-line-strong"
            >
              <CompareIcon size={14} />
              {picks.length > 2 ? `Compare (${pair.length}/2)` : 'Compare'}
            </button>
          )}
        </div>
        {error && <TrayError message={error} />}
        <div className="flex gap-2 overflow-x-auto pb-2.5 [scrollbar-width:thin]">
          {picks.map((pick) => (
            <PickTile
              key={pick.pick}
              pick={pick}
              busy={busy}
              selectable={picks.length > 2}
              marked={live.includes(pick.pick)}
              onMark={() => toggleMark(pick.pick)}
              onGoesWith={() => onGoesWith(pick)}
              onRemove={() => onRemove(pick)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function PickTile({
  pick,
  busy,
  selectable,
  marked,
  onMark,
  onGoesWith,
  onRemove,
}: {
  pick: PickView
  busy: boolean
  selectable: boolean
  marked: boolean
  onMark: () => void
  onGoesWith: () => void
  onRemove: () => void
}) {
  const [imgFailed, setImgFailed] = useState(false)
  return (
    // The ring marks the pick "it" refers to: the last one ticked or asked
    // about. A tick is silent, so it is not labelled as a question.
    <div
      title={pick.focused ? 'The pick "it" refers to' : undefined}
      className={`flex w-60 shrink-0 items-center gap-2.5 rounded-xl border bg-surface p-2 shadow-card ${
        pick.focused ? 'border-clay ring-1 ring-clay/25' : marked ? 'border-clay/60' : 'border-line'
      }`}
    >
      {selectable && (
        <input
          type="checkbox"
          checked={marked}
          onChange={onMark}
          disabled={busy}
          aria-label={`Compare ${pick.name_english}`}
          className="h-4 w-4 shrink-0 accent-[var(--color-clay)]"
        />
      )}
      <div className="h-11 w-11 shrink-0 overflow-hidden rounded-lg bg-canvas">
        {!imgFailed && pick.image_url ? (
          <img
            src={pick.image_url}
            alt=""
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium text-ink" title={pick.name_english}>
          {pick.name_english}
        </div>
        <div className="text-[11px] text-muted">{money(pick.price_amount, pick.price_unit)}</div>
      </div>
      <button
        onClick={onGoesWith}
        disabled={busy}
        aria-label={`What goes with ${pick.name_english}`}
        title="What goes with it"
        className="inline-flex shrink-0 items-center gap-1 rounded-full border border-clay/30 px-2 py-1 text-[11px] font-semibold text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <SparkIcon size={12} />
        Goes with
      </button>
      <button
        onClick={onRemove}
        disabled={busy}
        aria-label={`Remove ${pick.name_english} from your picks`}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-rose disabled:cursor-not-allowed disabled:opacity-50"
      >
        <CloseIcon size={13} />
      </button>
    </div>
  )
}

function TrayError({ message }: { message: string }) {
  return (
    <div className="mx-auto mb-2 max-w-3xl rounded-lg border border-amber/25 bg-amber/10 px-3 py-1.5 text-xs text-amber">
      {message}
    </div>
  )
}
