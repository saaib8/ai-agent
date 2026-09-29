import { useState } from 'react'
import type { GroundedProduct } from '../../api/types'
import { dimensionsLine, humanise, money } from '../../lib/format'

/**
 * The pick the customer just chose, drawn above what goes with it.
 *
 * Its facts are on the card, not in the reply: the reply frames it and
 * introduces the companions beneath. Wide rather than a grid tile, so it reads
 * as the subject of the turn and not as one of the suggestions.
 */
export function FocusCard({
  product,
  label = 'Your pick',
  note,
}: {
  product: GroundedProduct
  label?: string
  note?: string
}) {
  const [imgFailed, setImgFailed] = useState(false)
  const { commerce } = product
  const kind = commerce.subcategory ?? commerce.category
  const price = money(product.price_amount, product.price_unit)
  const dims = dimensionsLine(product.dimensions)

  return (
    <div className="flex overflow-hidden rounded-2xl border border-clay/40 bg-surface shadow-card ring-1 ring-clay/15">
      <div className="w-32 shrink-0 bg-canvas sm:w-40">
        {!imgFailed && product.image_url ? (
          <img
            src={product.image_url}
            alt={product.name_english}
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 p-3.5">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-clay">
          {label}
        </span>
        <h4 className="text-sm font-semibold leading-snug text-ink">{product.name_english}</h4>
        {price && <div className="text-[15px] font-semibold text-clay">{price}</div>}
        {note && <div className="text-[12px] font-medium text-ink">{note}</div>}
        <div className="flex flex-wrap gap-1.5 text-[11px] text-muted">
          {kind && (
            <span className="rounded-full border border-clay/20 bg-clay-soft px-2 py-0.5 text-clay">
              {humanise(kind)}
            </span>
          )}
          {commerce.seating_capacity != null && (
            <span className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {commerce.seating_capacity}-seat
            </span>
          )}
          {product.main_color && (
            <span className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {humanise(product.main_color)}
            </span>
          )}
          {product.styles.map((style) => (
            <span key={style} className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {humanise(style)}
            </span>
          ))}
        </div>
        {dims && <div className="text-[11.5px] text-muted">{dims}</div>}
        {product.product_url && (
          <a
            href={product.product_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-auto text-xs font-medium text-clay hover:underline"
          >
            View product →
          </a>
        )}
      </div>
    </div>
  )
}
