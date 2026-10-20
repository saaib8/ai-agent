import { useRef, useState, type ReactNode } from 'react'
import type {
  BundleStatus,
  CardLabels,
  GroundedBundleItem,
  GroundedBundlePresentation,
  GroundedBundleTotals,
  RenderView,
} from '../../api/types'
import { cardWords, type CardWords } from '../../lib/cardWords'
import { humanise, money, toNumber } from '../../lib/format'
import { CloseIcon, ImageIcon, SwapIcon, UploadIcon } from '../icons'
import { ViewPicker } from './ViewPicker'

interface RoomBundleProps {
  room: GroundedBundlePresentation
  /** Start swapping this piece: opens the alternatives picker for its role.
   *  Called with the piece's card position and its role in customer words. */
  onSwapStart?: (bundleOrdinal: number, role: string) => void
  /** Take this piece out of the room. Called with its card position and its
   *  role in customer words. */
  onRemovePiece?: (bundleOrdinal: number, role: string) => void
  /** A turn is in flight — disable per-item actions. */
  busy?: boolean
  /** Present on the current package only: render it from a camera view. */
  onVisualize?: (view: RenderView, viewLabel: string) => void
  /** Present on the current package only: place it in the customer's own room -
   *  a new photo, or (null) the one this session already keeps. */
  onVisualizeInRoom?: (file: File | null) => void
  /** The room photo this session keeps, to show and reuse. */
  roomPhotoPreview?: string | null
  /** Header label. Defaults to the labels' "Room package"; a seating
   *  combination overrides it with "Seating option N". */
  label?: string
  /** The reply's words (`ChatResponse.labels`) - English without them. */
  labels?: CardLabels | null
  /** Hide the complete/partial/infeasible badge. A seating combination is
   *  complete by construction, so the badge is noise there. */
  hideStatus?: boolean
  /** Buttons drawn beneath the package - a seating option's choose and
   *  turn-down. */
  actions?: ReactNode
  /** Present on the current room package only: put every piece being bought
   *  in the cart, with its quantity. */
  onAddAllToCart?: () => void
}

/** What the pieces being bought come to, as the server worded it for the
 *  reply's language - English figures only from a server that sent none. */
export function newSpendText(totals: GroundedBundleTotals): string | null {
  return totals.display?.new_spend ?? money(totals.new_spend_total, totals.currency)
}

const STATUS_CLASS: Record<BundleStatus, string> = {
  complete: 'bg-sage/15 text-sage',
  partial: 'bg-amber/15 text-amber',
  infeasible: 'bg-rose/12 text-rose',
}

function Thumb({ item }: { item: GroundedBundleItem }) {
  const [failed, setFailed] = useState(false)
  if (failed || !item.image_url) {
    return <div className="h-12 w-12 shrink-0 rounded-lg bg-canvas" />
  }
  return (
    <img
      src={item.image_url}
      alt={item.display?.name ?? item.name_english}
      loading="lazy"
      onError={() => setFailed(true)}
      className="h-12 w-12 shrink-0 rounded-lg object-cover"
    />
  )
}

function BudgetBar({ spend, budget, within }: { spend: number; budget: number; within: boolean | null }) {
  const pct = budget > 0 ? Math.min(100, (spend / budget) * 100) : 0
  const over = within === false
  return (
    <div className="mt-1">
      <div className="h-2 w-full overflow-hidden rounded-full bg-canvas">
        <div
          className={`h-full rounded-full transition-all duration-500 ${over ? 'bg-rose' : 'bg-sage'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

export function RoomBundle({
  room,
  onSwapStart,
  onRemovePiece,
  busy = false,
  onVisualize,
  onVisualizeInRoom,
  roomPhotoPreview = null,
  label,
  hideStatus = false,
  actions,
  labels,
  onAddAllToCart,
}: RoomBundleProps) {
  const words = cardWords(labels).room
  const t = room.totals
  const spend = toNumber(t.new_spend_total)
  const budget = toNumber(t.budget_max_amount)
  // The server's words for the reply's language, as a store's widget shows
  // them; English figures only from a server that sent none.
  const grand = t.display?.new_spend ?? money(t.new_spend_total, t.currency)
  const budgetLabel = t.display?.budget ?? money(t.budget_max_amount, t.budget_currency)
  // "Add all to cart" adds the pieces being bought; a room that could not be
  // built, or holds only pieces they own, has nothing to add.
  // In their cart, the package is a record of how the room was designed: it
  // is no longer edited, and its pieces are already in.
  const record = !!room.in_cart
  const canAddAll =
    !record &&
    !!onAddAllToCart &&
    room.status !== 'infeasible' &&
    room.items.some((item) => item.acquisition === 'to_buy')

  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      <div className="flex items-center justify-between border-b border-line bg-canvas/60 px-4 py-3">
        <span className="text-sm font-semibold text-ink">{label ?? words.title}</span>
        {record ? (
          <span className="rounded-full bg-clay-soft px-2.5 py-0.5 text-[11px] font-semibold text-clay" dir="auto">
            {words.inCart}
          </span>
        ) : !hideStatus && (
          <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${STATUS_CLASS[room.status]}`}>
            {words.status[room.status]}
          </span>
        )}
      </div>

      <ul className="divide-y divide-line">
        {room.items.map((item) => {
          const kind = item.commerce.subcategory ?? item.commerce.category
          const shown = item.display
          const kindText = shown ? shown.kind : kind ? humanise(kind) : null
          const unit = shown ? shown.price : money(item.unit_price, item.price_unit)
          const line = shown ? shown.line_total : money(item.new_spend_line_total, item.price_unit)
          return (
            <li key={item.grounding_ref} className="flex items-center gap-3 px-4 py-3">
              <Thumb item={item} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink" dir="auto">
                  {shown?.name ?? item.name_english}
                  {item.quantity > 1 && <span className="text-muted"> ×{item.quantity}</span>}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                  {kindText && (
                    <span className="rounded-full bg-canvas px-2 py-0.5 text-[11px] text-muted">
                      {kindText}
                    </span>
                  )}
                  {item.locked && (
                    <span className="rounded-full bg-clay-soft px-2 py-0.5 text-[11px] font-medium text-clay">
                      {words.locked}
                    </span>
                  )}
                  {item.acquisition === 'already_owned' && (
                    <span className="rounded-full bg-canvas px-2 py-0.5 text-[11px] text-muted">{words.alreadyOwned}</span>
                  )}
                  {!record && onSwapStart && item.acquisition === 'to_buy' && !item.locked && (
                    <button
                      onClick={() => onSwapStart(item.grounding_ref, humanise(kind ?? 'item'))}
                      disabled={busy}
                      aria-label={words.swapPiece(kindText ?? humanise('item'))}
                      className="inline-flex items-center gap-1 rounded-full border border-clay/30 px-2 py-0.5 text-[11px] font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <SwapIcon size={12} />
                      {words.swap}
                    </button>
                  )}
                  {!record && onRemovePiece && (
                    <button
                      onClick={() => onRemovePiece(item.grounding_ref, humanise(kind ?? 'item'))}
                      disabled={busy}
                      aria-label={words.removePiece(kindText ?? humanise('item'))}
                      className="inline-flex items-center gap-1 rounded-full border border-line px-2 py-0.5 text-[11px] font-medium text-muted transition hover:border-ink hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <CloseIcon size={12} />
                      {words.remove}
                    </button>
                  )}
                </div>
              </div>
              <div className="shrink-0 text-right">
                {unit && (
                  <div className="text-[11px] text-muted" dir="auto">
                    {words.each(unit)}
                  </div>
                )}
                <div className="text-sm font-medium text-ink" dir="auto">
                  {item.acquisition === 'already_owned' ? '—' : (line ?? unit ?? '—')}
                </div>
              </div>
            </li>
          )
        })}
      </ul>

      <div className="space-y-1.5 px-4 py-4">
        {grand && (
          <div className="flex items-baseline justify-between">
            <span className="text-sm text-muted">{words.newSpend}</span>
            <span className="text-lg font-semibold text-ink" dir="auto">
              {grand}
            </span>
          </div>
        )}
        {budgetLabel && (
          <>
            <div className="flex items-baseline justify-between text-xs">
              <span className="text-muted" dir="auto">
                {words.budget(budgetLabel, t.budget_max_exclusive)}
              </span>
              <span className={t.within_budget === false ? 'font-medium text-rose' : 'font-medium text-sage'}>
                {t.within_budget === false ? words.overBudget : words.withinBudget}
              </span>
            </div>
            {spend != null && budget != null && (
              <BudgetBar spend={spend} budget={budget} within={t.within_budget} />
            )}
          </>
        )}
        {t.total_unavailable && (
          <div className="pt-1 text-xs italic text-muted" dir="auto">
            {words.totalUnavailable(t.total_unavailable)}
          </div>
        )}
      </div>

      {(actions || canAddAll) && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line bg-canvas/60 px-4 py-3">
          {canAddAll && (
            <button
              onClick={onAddAllToCart}
              disabled={busy}
              className="rounded-full bg-ink px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
              dir="auto"
            >
              {words.addAllToCart(newSpendText(t))}
            </button>
          )}
          {actions}
        </div>
      )}
      {onVisualize && room.items.length > 0 && (
        <VisualizeBar
          words={words}
          busy={busy}
          onVisualize={onVisualize}
          onVisualizeInRoom={onVisualizeInRoom}
          roomPhotoPreview={roomPhotoPreview}
        />
      )}
    </div>
  )
}

function VisualizeBar({
  words,
  busy,
  onVisualize,
  onVisualizeInRoom,
  roomPhotoPreview,
}: {
  words: CardWords['room']
  busy: boolean
  onVisualize: (view: RenderView, viewLabel: string) => void
  onVisualizeInRoom?: (file: File | null) => void
  roomPhotoPreview: string | null
}) {
  const [view, setView] = useState<RenderView>('corner')
  const label = words.views[view]
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line bg-canvas/60 px-4 py-3">
      <ViewPicker
        value={view}
        onChange={setView}
        disabled={busy}
        labels={words.views}
        ariaLabel={words.cameraView}
      />
      <div className="flex flex-wrap items-center gap-2">
        {onVisualizeInRoom && (
          <RoomPhotoButton
            words={words}
            busy={busy}
            preview={roomPhotoPreview}
            onVisualizeInRoom={onVisualizeInRoom}
          />
        )}
        <button
          onClick={() => onVisualize(view, label)}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-full bg-clay px-4 py-2 text-sm font-medium text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:bg-line-strong"
        >
          <ImageIcon size={16} />
          {words.visualize}
        </button>
      </div>
    </div>
  )
}

/**
 * Put the package in the customer's own room. The first time it asks for a
 * photo; after that the session keeps the emptied room, so the pieces can be
 * placed in it again - or a different photo chosen.
 */
function RoomPhotoButton({
  words,
  busy,
  preview,
  onVisualizeInRoom,
}: {
  words: CardWords['room']
  busy: boolean
  preview: string | null
  onVisualizeInRoom: (file: File | null) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const pick = () => input.current?.click()
  const secondary =
    'inline-flex items-center gap-2 rounded-full border border-clay/40 bg-surface px-4 py-2 text-sm font-medium text-clay transition hover:border-clay hover:bg-clay-soft/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50'
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          e.target.value = ''
          if (file) onVisualizeInRoom(file)
        }}
      />
      {preview ? (
        <>
          <button type="button" onClick={() => onVisualizeInRoom(null)} disabled={busy} className={secondary}>
            <img src={preview} alt="" className="-ml-2 h-6 w-6 rounded-full object-cover" />
            {words.placeInMyRoom}
          </button>
          <button
            type="button"
            onClick={pick}
            disabled={busy}
            className="rounded-full px-2 py-2 text-xs font-medium text-muted transition hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          >
            {words.changePhoto}
          </button>
        </>
      ) : (
        <button type="button" onClick={pick} disabled={busy} className={secondary}>
          <UploadIcon size={16} />
          {words.uploadRoomPhoto}
        </button>
      )}
    </>
  )
}
