import type { GroundedProduct } from '../../api/types'
import { ProductCard } from './ProductCard'
import type { AlternativePick, CardCompare, CardSelect } from './ProductCard'

export interface SearchRefineControls {
  /** Re-run the search, excluding everything on screen — a different page. */
  onShowMore: () => void
  /** Drop one product and re-run, so it does not come back. */
  onExclude: (product: GroundedProduct) => void
}

interface ProductGridProps {
  products: GroundedProduct[]
  /** Present while choosing a room replacement: each card gains a "Use this". */
  pick?: AlternativePick
  /** Present on the latest search grid: a "different options" button and a
   *  per-card "not this one". Absent while picking a room replacement. */
  refine?: SearchRefineControls
  /** Present on the search results on screen: each card can be ticked. */
  selection?: GridSelection
  /** The first card is the closest to what they described. */
  bestMatch?: boolean
  /** Present on search results: each card can be checked for comparison. */
  compare?: GridCompare
  busy?: boolean
}

/** Which cards are checked for comparison, and which may still be. */
export interface GridCompare {
  compareFor: (product: GroundedProduct) => CardCompare | undefined
}

/** Which cards are picked, by card number, and how to tick one. */
export interface GridSelection {
  pickedOrdinals: ReadonlySet<number>
  onToggle: (product: GroundedProduct) => void
}

export function ProductGrid({
  products,
  pick,
  refine,
  selection,
  bestMatch = false,
  compare,
  busy,
}: ProductGridProps) {
  if (products.length === 0) return null
  const showRefine = refine && !pick
  return (
    <div className="flex flex-col gap-2.5">
      {pick && (
        <div className="rounded-xl border border-clay/25 bg-clay-soft/50 px-3.5 py-2 text-xs font-medium text-clay">
          Choose a replacement {pick.role} — tap “Use this” on the one you like, and I&apos;ll rebuild the
          room around it.
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {products.map((p, index) => (
          <ProductCard
            key={p.grounding_ref}
            product={p}
            pick={pick}
            onExclude={refine?.onExclude}
            select={cardSelect(p, selection, !!busy)}
            compare={compare?.compareFor(p)}
            bestMatch={bestMatch && index === 0}
          />
        ))}
      </div>
      {showRefine && (
        <button
          onClick={refine.onShowMore}
          disabled={busy}
          className="self-start rounded-lg border border-line px-3.5 py-2 text-xs font-semibold text-ink transition hover:border-line-strong hover:bg-canvas focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:opacity-50"
        >
          Show me different options
        </button>
      )}
    </div>
  )
}

function cardSelect(
  product: GroundedProduct,
  selection: GridSelection | undefined,
  busy: boolean,
): CardSelect | undefined {
  if (!selection || product.presented_ordinal == null) return undefined
  return {
    picked: selection.pickedOrdinals.has(product.presented_ordinal),
    onToggle: selection.onToggle,
    disabled: busy,
  }
}
