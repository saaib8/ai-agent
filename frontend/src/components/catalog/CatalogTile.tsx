import { useState } from 'react'
import type { CardLabels, CatalogItem } from '../../api/types'
import { cardWords } from '../../lib/cardWords'
import { productText } from '../../lib/productWords'
import { CheckIcon, PlusIcon } from '../icons'
import { QuantityStepper } from './QuantityStepper'

interface CatalogTileProps {
  item: CatalogItem
  /** 0 when not in the selection. */
  quantity: number
  maxQuantity: number
  /** False once the selection holds as many products as a render can take. */
  canAdd: boolean
  maxProducts: number
  onAdd: () => void
  onQuantity: (quantity: number) => void
  /** The conversation's latest words (`ChatResponse.labels`). The tile's
   *  own text comes worded from the server (`display`). */
  labels?: CardLabels | null
}

/** A product in the browse grid: add it, then set how many. */
export function CatalogTile({
  item,
  quantity,
  maxQuantity,
  canAdd,
  maxProducts,
  onAdd,
  onQuantity,
  labels,
}: CatalogTileProps) {
  const [imgFailed, setImgFailed] = useState(false)
  const selected = quantity > 0
  const text = productText(item)
  const dims = text.size
  const words = cardWords(labels).tile

  return (
    <div
      className={`group flex flex-col overflow-hidden rounded-2xl border bg-surface shadow-card transition ${
        selected ? 'border-clay ring-1 ring-clay/30' : 'border-line hover:border-line-strong'
      }`}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-canvas">
        {!imgFailed && item.image_url ? (
          <img
            src={item.image_url}
            alt={text.name}
            loading="lazy"
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]"
          />
        ) : (
          <div className="h-full w-full" />
        )}
        {selected && (
          <span className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full bg-clay text-white shadow-card">
            <CheckIcon size={14} />
          </span>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-1.5 p-3">
        <h4
          className="line-clamp-2 text-[13px] font-medium leading-snug text-ink"
          title={text.name}
          dir="auto"
        >
          {text.name}
        </h4>
        <div className="text-sm font-semibold text-clay">{text.price}</div>
        {text.kind && <div className="text-[11px] capitalize text-muted">{text.kind}</div>}
        {dims && <div className="text-[11px] text-muted">{dims}</div>}

        <div className="mt-auto flex items-center justify-between gap-2 pt-1.5">
          {selected ? (
            <QuantityStepper
              value={quantity}
              max={maxQuantity}
              onChange={onQuantity}
              label={text.name}
              labels={labels}
              size="sm"
            />
          ) : (
            <button
              type="button"
              onClick={onAdd}
              disabled={!canAdd}
              title={canAdd ? undefined : words.roomLimit(maxProducts)}
              className="inline-flex items-center gap-1 rounded-full border border-clay/30 px-3 py-1.5 text-xs font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:bg-transparent"
            >
              <PlusIcon size={13} />
              {words.add}
            </button>
          )}
          <a
            href={item.product_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] font-medium text-muted hover:text-clay hover:underline"
          >
            {words.view}
          </a>
        </div>
      </div>
    </div>
  )
}
