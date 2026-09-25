import { useState } from 'react'
import { RENDER_VIEWS } from '../../api/types'
import type {
  BundleStatus,
  GroundedBundleItem,
  GroundedBundlePresentation,
  RenderView,
} from '../../api/types'
import { humanise, money, toNumber } from '../../lib/format'
import { ImageIcon, SwapIcon } from '../icons'
import { ViewPicker } from './ViewPicker'

interface RoomBundleProps {
  room: GroundedBundlePresentation
  /** Start swapping this piece: opens the alternatives picker for its role.
   *  Called with the piece's card position and its role in customer words. */
  onSwapStart?: (bundleOrdinal: number, role: string) => void
  /** A turn is in flight — disable per-item actions. */
  busy?: boolean
  /** Present on the current package only: render it from a camera view. */
  onVisualize?: (view: RenderView, viewLabel: string) => void
}

const STATUS_META: Record<BundleStatus, { label: string; cls: string }> = {
  complete: { label: 'Complete', cls: 'bg-sage/15 text-sage' },
  partial: { label: 'Partial', cls: 'bg-amber/15 text-amber' },
  infeasible: { label: 'Infeasible', cls: 'bg-rose/12 text-rose' },
}

function Thumb({ item }: { item: GroundedBundleItem }) {
  const [failed, setFailed] = useState(false)
  if (failed || !item.image_url) {
    return <div className="h-12 w-12 shrink-0 rounded-lg bg-canvas" />
  }
  return (
    <img
      src={item.image_url}
      alt={item.name_english}
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

export function RoomBundle({ room, onSwapStart, busy = false, onVisualize }: RoomBundleProps) {
  const status = STATUS_META[room.status]
  const t = room.totals
  const spend = toNumber(t.new_spend_total)
  const budget = toNumber(t.budget_max_amount)
  const grand = money(t.new_spend_total, t.currency)
  const budgetLabel = money(t.budget_max_amount, t.budget_currency)

  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      <div className="flex items-center justify-between border-b border-line bg-canvas/60 px-4 py-3">
        <span className="text-sm font-semibold text-ink">Room package</span>
        <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${status.cls}`}>
          {status.label}
        </span>
      </div>

      <ul className="divide-y divide-line">
        {room.items.map((item) => {
          const kind = item.commerce.subcategory ?? item.commerce.category
          const unit = money(item.unit_price, item.price_unit)
          const line = money(item.new_spend_line_total, item.price_unit)
          return (
            <li key={item.grounding_ref} className="flex items-center gap-3 px-4 py-3">
              <Thumb item={item} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink">
                  {item.name_english}
                  {item.quantity > 1 && <span className="text-muted"> ×{item.quantity}</span>}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                  {kind && (
                    <span className="rounded-full bg-canvas px-2 py-0.5 text-[11px] text-muted">
                      {humanise(kind)}
                    </span>
                  )}
                  {item.locked && (
                    <span className="rounded-full bg-clay-soft px-2 py-0.5 text-[11px] font-medium text-clay">
                      locked
                    </span>
                  )}
                  {item.acquisition === 'already_owned' && (
                    <span className="rounded-full bg-canvas px-2 py-0.5 text-[11px] text-muted">already owned</span>
                  )}
                  {onSwapStart && item.acquisition === 'to_buy' && !item.locked && (
                    <button
                      onClick={() => onSwapStart(item.grounding_ref, humanise(kind ?? 'item'))}
                      disabled={busy}
                      aria-label={`Swap the ${humanise(kind ?? 'item')}`}
                      className="inline-flex items-center gap-1 rounded-full border border-clay/30 px-2 py-0.5 text-[11px] font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <SwapIcon size={12} />
                      Swap
                    </button>
                  )}
                </div>
              </div>
              <div className="shrink-0 text-right">
                {unit && <div className="text-[11px] text-muted">{unit} ea</div>}
                <div className="text-sm font-medium text-ink">
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
            <span className="text-sm text-muted">New spend</span>
            <span className="text-lg font-semibold text-ink">{grand}</span>
          </div>
        )}
        {budgetLabel && (
          <>
            <div className="flex items-baseline justify-between text-xs">
              <span className="text-muted">
                Budget {t.budget_max_exclusive ? '<' : '≤'} {budgetLabel}
              </span>
              <span className={t.within_budget === false ? 'font-medium text-rose' : 'font-medium text-sage'}>
                {t.within_budget === false ? 'over budget' : 'within budget'}
              </span>
            </div>
            {spend != null && budget != null && (
              <BudgetBar spend={spend} budget={budget} within={t.within_budget} />
            )}
          </>
        )}
        {t.total_unavailable && (
          <div className="pt-1 text-xs italic text-muted">
            Total unavailable: {humanise(t.total_unavailable)}
          </div>
        )}
      </div>

      {onVisualize && room.items.length > 0 && <VisualizeBar busy={busy} onVisualize={onVisualize} />}
    </div>
  )
}

function VisualizeBar({
  busy,
  onVisualize,
}: {
  busy: boolean
  onVisualize: (view: RenderView, viewLabel: string) => void
}) {
  const [view, setView] = useState<RenderView>('corner')
  const label = RENDER_VIEWS.find((v) => v.value === view)?.label ?? 'Corner'
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line bg-canvas/60 px-4 py-3">
      <ViewPicker value={view} onChange={setView} disabled={busy} />
      <button
        onClick={() => onVisualize(view, label)}
        disabled={busy}
        className="inline-flex items-center gap-2 rounded-full bg-clay px-4 py-2 text-sm font-medium text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:bg-line-strong"
      >
        <ImageIcon size={16} />
        Visualize
      </button>
    </div>
  )
}
