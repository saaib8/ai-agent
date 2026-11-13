import type { GroundedProduct, ProductAction, ReplyChoice } from '../../api/types'

/** The grid of what goes with a pick (`kind_cards`): each product stands for
 *  its kind, drawn small - its image and the kind's name, nothing else. A tap
 *  is the kind's chip: the same words and the same `companion` action, so it
 *  opens that kind's page exactly as the chip would. */
export function KindCards({
  products,
  names,
  chips,
  onPick,
  busy,
}: {
  products: GroundedProduct[]
  /** The kinds in the customer's words, by subcategory (`kind_names`). */
  names: Record<string, string>
  chips: ReplyChoice[]
  onPick?: (value: string, action: ProductAction) => void
  busy?: boolean
}) {
  if (products.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2.5">
      {products.map((p) => {
        const chip = kindChip(chips, p.commerce.subcategory)
        const name =
          names[p.commerce.subcategory ?? ''] ?? chip?.label ?? p.display?.kind ?? p.commerce.subcategory ?? ''
        const action: ProductAction = chip?.product_action?.kind === 'companion'
          ? chip.product_action
          : { kind: 'companion', category: p.commerce.category ?? '', subcategory: p.commerce.subcategory ?? '' }
        return (
          <button
            key={p.grounding_ref}
            type="button"
            disabled={busy || !onPick || !p.commerce.subcategory || !p.commerce.category}
            onClick={() => onPick?.(chip?.value ?? name, action)}
            className="flex w-24 flex-col items-center gap-1.5 rounded-xl border border-line bg-surface p-2 text-center transition hover:border-line-strong hover:bg-canvas focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:opacity-60"
          >
            <img
              src={p.image_url}
              alt=""
              loading="lazy"
              className="h-16 w-16 rounded-lg bg-canvas object-cover"
            />
            <span className="line-clamp-2 text-xs font-medium capitalize leading-4 text-ink">{name}</span>
          </button>
        )
      })}
    </div>
  )
}

function kindChip(chips: ReplyChoice[], subcategory: string | null | undefined): ReplyChoice | undefined {
  if (!subcategory) return undefined
  return chips.find(
    (c) => c.product_action?.kind === 'companion' && c.product_action.subcategory === subcategory,
  )
}

/** The kind chips a reply's kind cards already stand for - drawn once, as cards. */
export function kindsOnCards(products: GroundedProduct[] | undefined): Set<string> {
  return new Set((products ?? []).map((p) => p.commerce.subcategory ?? '').filter(Boolean))
}
