import type { CommerceClassification, NormalisedDimensions, ProductDisplay } from '../api/types'
import { dimensionsLine, humanise, money } from './format'

/** What a product card shows of a product. */
export interface ProductText {
  name: string
  kind: string | null
  color: string | null
  styles: string[]
  seats: string | null
  price: string | null
  size: string | null
}

/** Anything the console draws as a product - a card, a pick, a catalogue tile. */
export interface ShownProduct {
  name_english: string
  commerce?: CommerceClassification
  main_color?: string | null
  styles?: string[]
  price_amount?: string
  price_unit?: string
  dimensions?: NormalisedDimensions
  display?: ProductDisplay | null
}

/** A product as the server worded it for the reply's language (`display`) -
 *  the same text a store's widget shows. A server that sent none is read in
 *  English, as the console always showed it. Display only: what is sent back
 *  always uses the English values. */
export function productText(product: ShownProduct): ProductText {
  if (product.display) return product.display
  const kind = product.commerce?.subcategory ?? product.commerce?.category ?? null
  const seats = product.commerce?.seating_capacity
  return {
    name: product.name_english,
    kind: kind ? humanise(kind) : null,
    color: product.main_color ? humanise(product.main_color) : null,
    styles: (product.styles ?? []).map(humanise),
    seats: seats != null ? `${seats}-seat` : null,
    price: money(product.price_amount, product.price_unit),
    size: dimensionsLine(product.dimensions),
  }
}

/** A filter option's label: the server's, in the requested language. */
export function optionLabel(
  option: { value: string; label?: string | null },
  english: (value: string) => string,
): string {
  return option.label || english(option.value)
}
