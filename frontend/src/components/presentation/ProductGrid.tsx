import type { GroundedProduct } from '../../api/types'
import { ProductCard } from './ProductCard'

export function ProductGrid({ products }: { products: GroundedProduct[] }) {
  if (products.length === 0) return null
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {products.map((p) => (
        <ProductCard key={p.grounding_ref} product={p} />
      ))}
    </div>
  )
}
