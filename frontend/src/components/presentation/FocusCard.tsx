import { useState } from 'react'
import type { CardLabels, GroundedProduct } from '../../api/types'
import { cardWords } from '../../lib/cardWords'
import { productText } from '../../lib/productWords'

/**
 * The pick the customer just chose, drawn above what goes with it.
 *
 * Its facts are on the card, not in the reply: the reply frames it and
 * introduces the companions beneath. Wide rather than a grid tile, so it reads
 * as the subject of the turn and not as one of the suggestions.
 */
export function FocusCard({
  product,
  labels,
}: {
  product: GroundedProduct
  /** The reply's words around a card (`ChatResponse.labels`). */
  labels?: CardLabels | null
}) {
  const [imgFailed, setImgFailed] = useState(false)
  const text = productText(product)
  const words = cardWords(labels)
  const price = text.price
  const dims = text.size

  return (
    <div className="flex overflow-hidden rounded-2xl border border-clay/40 bg-surface shadow-card ring-1 ring-clay/15">
      <div className="w-32 shrink-0 bg-canvas sm:w-40">
        {!imgFailed && product.image_url ? (
          <img
            src={product.image_url}
            alt={text.name}
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 p-3.5">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-clay">
          {words.yourPick}
        </span>
        <h4 className="text-sm font-semibold leading-snug text-ink" dir="auto">
          {text.name}
        </h4>
        {price && <div className="text-[15px] font-semibold text-clay">{price}</div>}
        <div className="flex flex-wrap gap-1.5 text-[11px] text-muted">
          {text.kind && (
            <span className="rounded-full border border-clay/20 bg-clay-soft px-2 py-0.5 text-clay">
              {text.kind}
            </span>
          )}
          {text.seats && (
            <span className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {text.seats}
            </span>
          )}
          {text.color && (
            <span className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {text.color}
            </span>
          )}
          {text.styles.map((style) => (
            <span key={style} className="rounded-full border border-line bg-canvas px-2 py-0.5">
              {style}
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
            {words.viewProduct}
          </a>
        )}
      </div>
    </div>
  )
}
