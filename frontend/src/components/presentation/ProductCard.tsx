import { useState } from 'react'
import type { ReactNode } from 'react'
import type { GroundedProduct } from '../../api/types'
import { dimensionsLine, humanise, money } from '../../lib/format'
import { CheckIcon, HeartIcon, PlusIcon, SparkIcon } from '../icons'

function Chip({ children, tone = 'default' }: { children: ReactNode; tone?: 'default' | 'accent' }) {
  const cls =
    tone === 'accent'
      ? 'bg-clay-soft text-clay border-clay/20'
      : 'bg-canvas text-muted border-line'
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] leading-4 ${cls}`}>
      {children}
    </span>
  )
}

export interface AlternativePick {
  role: string
  onPick: (alternativeOrdinal: number) => void
}

/** Checking a card for a side-by-side comparison. */
export interface CardCompare {
  checked: boolean
  /** Another kind of product is already checked, or two already are. */
  disabled: boolean
  onToggle: (product: GroundedProduct) => void
  /** Why it cannot be checked, when it cannot. */
  hint?: string
}

/** ♡ on a card: kept in the liked list, apart from the picks. */
export interface CardLike {
  liked: boolean
  onToggle: (product: GroundedProduct) => void
}

/** Ticking a card into the customer's picks. */
export interface CardSelect {
  picked: boolean
  onToggle: (product: GroundedProduct) => void
  disabled: boolean
}

export function ProductCard({
  product,
  pick,
  onExclude,
  select,
  compare,
  like,
  onMoreLikeThis,
  busy = false,
}: {
  product: GroundedProduct
  pick?: AlternativePick
  /** Present on a fresh search grid: drops this one and re-runs, so it does
   *  not come back. Absent while picking a room replacement. */
  onExclude?: (product: GroundedProduct) => void
  /** Present on the search results on screen: tick it into the picks. */
  select?: CardSelect
  /** Present on search results: check it to compare with a similar one. */
  compare?: CardCompare
  /** Present when the card can be liked. */
  like?: CardLike
  /** Present when a similarity search can start from this card. */
  onMoreLikeThis?: (product: GroundedProduct) => void
  /** A turn is in flight: actions that start one wait for it. */
  busy?: boolean
}) {
  const [imgFailed, setImgFailed] = useState(false)
  const { commerce } = product
  const price = money(product.price_amount, product.price_unit)
  const dims = dimensionsLine(product.dimensions)
  const widened = product.relaxation_depth != null && product.relaxation_depth > 0
  const kind = commerce.subcategory ?? commerce.category

  const picked = select?.picked ?? false

  return (
    <div
      className={`group flex flex-col overflow-hidden rounded-2xl border bg-surface shadow-card transition duration-200 hover:shadow-soft ${
        picked ? 'border-clay ring-1 ring-clay/30' : 'border-line hover:border-line-strong'
      }`}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-canvas">
        {!imgFailed && product.image_url ? (
          <img
            src={product.image_url}
            alt={product.name_english}
            loading="lazy"
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted/50">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="m3 15 5-5 4 4 3-3 6 6" />
              <circle cx="8.5" cy="9.5" r="1.5" />
            </svg>
          </div>
        )}

        {product.presented_ordinal != null && (
          <div className="absolute left-2 top-2 flex items-center gap-1">
            <span className="rounded-full bg-ink/75 px-2 py-0.5 text-[11px] font-medium text-white backdrop-blur-sm">
              #{product.presented_ordinal}
            </span>
          </div>
        )}
        {like && !pick && (
          <button
            onClick={() => like.onToggle(product)}
            aria-pressed={like.liked}
            aria-label={like.liked ? `Unlike ${product.name_english}` : `Like ${product.name_english}`}
            title={like.liked ? 'Liked' : 'Like'}
            className={`absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full bg-surface/90 shadow-card backdrop-blur-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 ${
              like.liked ? 'text-rose' : 'text-muted hover:text-rose'
            }`}
          >
            <HeartIcon size={16} filled={like.liked} />
          </button>
        )}
        {widened && (
          <span className="absolute right-2 top-2 rounded-full bg-amber px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white">
            widened
          </span>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3.5">
        <h4 className="text-sm font-medium leading-snug text-ink" title={product.name_english}>
          {product.name_english}
        </h4>
        {price && <div className="text-[15px] font-semibold text-clay">{price}</div>}

        <div className="flex flex-wrap gap-1.5">
          {kind && <Chip tone="accent">{humanise(kind)}</Chip>}
          {commerce.seating_capacity != null && <Chip>{commerce.seating_capacity}-seat</Chip>}
          {product.main_color && <Chip>{humanise(product.main_color)}</Chip>}
          {product.styles.map((s) => (
            <Chip key={s}>{humanise(s)}</Chip>
          ))}
        </div>

        {dims && <div className="text-[11.5px] text-muted">{dims}</div>}

        <div className="mt-auto flex flex-col gap-2 pt-1">
          {compare && !pick && product.presented_ordinal != null && (
            <label
              title={compare.disabled && !compare.checked ? compare.hint : undefined}
              className={`inline-flex w-fit items-center gap-2 text-xs font-medium ${
                compare.disabled && !compare.checked
                  ? 'cursor-not-allowed text-muted/50'
                  : 'cursor-pointer text-muted hover:text-ink'
              }`}
            >
              <input
                type="checkbox"
                checked={compare.checked}
                disabled={compare.disabled && !compare.checked}
                onChange={() => compare.onToggle(product)}
                className="h-3.5 w-3.5 accent-[var(--color-clay)]"
              />
              Compare
            </label>
          )}
          {select && !pick && product.presented_ordinal != null && (
            <button
              onClick={() => select.onToggle(product)}
              disabled={select.disabled}
              aria-pressed={picked}
              className={`inline-flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:opacity-50 ${
                picked
                  ? 'bg-clay text-white hover:bg-clay-hover'
                  : 'border border-clay/30 text-clay hover:border-clay hover:bg-clay hover:text-white'
              }`}
            >
              {picked ? <CheckIcon size={13} /> : <PlusIcon size={13} />}
              {picked ? 'Selected' : 'Select'}
            </button>
          )}
          {pick && (
            <button
              onClick={() => pick.onPick(product.presented_ordinal ?? product.grounding_ref)}
              className="w-full rounded-lg bg-clay px-3 py-2 text-xs font-semibold text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40"
            >
              Use this {pick.role}
            </button>
          )}
          {!pick && onMoreLikeThis && product.presented_ordinal != null && (
            <button
              onClick={() => onMoreLikeThis(product)}
              disabled={busy}
              className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-medium text-ink transition hover:border-clay/50 hover:text-clay focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <SparkIcon size={13} />
              More like this
            </button>
          )}
          {!pick && onExclude && product.presented_ordinal != null && (
            <button
              onClick={() => onExclude(product)}
              className="w-full rounded-lg border border-line px-3 py-2 text-xs font-medium text-muted transition hover:border-line-strong hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30"
            >
              Not this one
            </button>
          )}
          {product.product_url && (
            <a
              href={product.product_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs font-medium text-clay hover:underline"
            >
              View product →
            </a>
          )}
        </div>
      </div>
    </div>
  )
}
