import { useState } from 'react'
import type { GroundedProduct } from '../../api/types'
import { dimensionsLine, humanise, money } from '../../lib/format'
import { useWidget } from '../context'
import { Icon } from '../icons'

interface ProductCardProps {
  product: GroundedProduct
  /** The result list this card belongs to; null for cards no position acts on
   *  (a product opened on its own, the shopper's own picks shown back). */
  listRevision: number | null
  /** Only the newest list offers "Not this one" and swap choices. */
  latest: boolean
  bestMatch?: boolean
}

export function ProductImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false)
  if (!src || failed) {
    return (
      <span className="ph">
        <Icon name="image" size={22} />
      </span>
    )
  }
  return <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />
}

export function ProductCard({ product, listRevision, latest, bestMatch }: ProductCardProps) {
  const { agent } = useWidget()
  const ordinal = product.presented_ordinal
  const actionable = listRevision != null && ordinal != null
  const pick = actionable
    ? agent.picks.find((p) =>
        (p.positions ?? []).some((pos) => pos.list_revision === listRevision && pos.ordinal === ordinal),
      )
    : undefined
  const comparing = pick != null && agent.comparing.includes(pick.pick)
  const busy = agent.sending || agent.picking
  const kind = product.commerce.subcategory ?? product.commerce.category
  const dims = dimensionsLine(product.dimensions)
  const choosing = agent.swap !== null && latest && actionable

  return (
    <article className={`pcard${pick ? ' picked' : ''}`}>
      <a
        className="pimg"
        href={product.product_url || undefined}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`${product.name_english} — open in the store`}
      >
        <ProductImage src={product.image_url} alt="" />
        {bestMatch && <span className="tag">Best match</span>}
      </a>
      {actionable && latest && !choosing && (
        <button
          className="dismiss"
          onClick={() => agent.exclude(product)}
          disabled={busy}
          aria-label={`Not this one: ${product.name_english}`}
          title="Not this one"
        >
          <Icon name="close" size={14} strokeWidth={2} />
        </button>
      )}
      <div className="pbody">
        {kind && <span className="pcat">{humanise(kind)}</span>}
        <a
          className="pname"
          href={product.product_url || undefined}
          target="_blank"
          rel="noopener noreferrer"
        >
          {product.name_english}
        </a>
        {(product.main_color || product.styles.length > 0) && (
          <div className="pchips">
            {product.main_color && <span className="pchip on">{product.main_color}</span>}
            {product.styles.slice(0, 1).map((style) => (
              <span key={style} className="pchip">
                {humanise(style)}
              </span>
            ))}
          </div>
        )}
        {dims && <span className="pdims">{dims}</span>}
        <div className="pfoot">
          <span className="price">{money(product.price_amount, product.price_unit)}</span>
          {choosing ? null : actionable ? (
            <>
              <button
                className="add-btn"
                onClick={() => void agent.toggleBasket(product, listRevision!)}
                disabled={busy}
                aria-pressed={!!pick}
                aria-label={pick ? 'Remove from basket' : 'Add to basket'}
                title={pick ? 'In your basket' : 'Add to basket'}
              >
                <Icon name={pick ? 'check' : 'plus'} size={16} strokeWidth={2} />
              </button>
            </>
          ) : null}
        </div>
        {!choosing && actionable && (
          <label className={`compare-check${comparing ? ' on' : ''}`}>
            <input
              type="checkbox"
              checked={comparing}
              onChange={() => void agent.toggleCompare(product, listRevision!)}
              disabled={busy}
            />
            Compare
          </label>
        )}
        {choosing && (
          <button className="use-btn" onClick={() => agent.chooseAlternative(ordinal!)} disabled={busy}>
            Use this one
          </button>
        )}
      </div>
    </article>
  )
}
