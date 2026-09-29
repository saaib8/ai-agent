import type { GroundedProduct } from '../api/types'

/** A card checked for comparison: which result list it is on, where, and the
 *  family it compares within. */
export interface CheckedCard {
  listRevision: number
  ordinal: number
  family: string
  name: string
  imageUrl: string | null
}

/** The family a product compares within - its reviewed group, or its own
 *  type. Null for an unclassified product, which cannot be compared. */
export function compareFamily(
  product: GroundedProduct,
  groups: Record<string, string>,
): string | null {
  const type = product.commerce.subcategory
  return type ? (groups[type] ?? type) : null
}
