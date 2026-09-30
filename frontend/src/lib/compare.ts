import type { GroundedProduct } from '../api/types'

/** How many products one comparison may cover until the server says - the
 *  server's own default, and the picks tray's size. */
export const DEFAULT_COMPARE_MAX = 10

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
