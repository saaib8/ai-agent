import type {
  CardLabels,
  ComparisonCell,
  GroundedProduct,
  ProductComparisonResult,
} from '../../api/types'
import { cardWords, type CardWords } from '../../lib/cardWords'
import { humanise } from '../../lib/format'
import { productText } from '../../lib/productWords'

const STATUS_STYLES: Record<string, string> = {
  different: 'bg-clay-soft text-clay',
  same: 'bg-sage/12 text-sage',
}

/** The measurements the backend reads per product type (CLAUDE.md 15.1). */
const SIZE_FIELDS = new Set(['length', 'overall_width', 'depth', 'height'])

/** A cell as this conversation reads it. The kind, colour and styles rows
 *  read from the product in that column, as the server worded it for the
 *  reply (`display`); every other cell is shown as the backend wrote it. */
function cellText(
  field: string,
  cell: ComparisonCell,
  product: GroundedProduct,
  words: CardWords,
): string {
  if (!cell.known || cell.value == null) return '—'
  if (!product.display) return cell.value
  const text = productText(product)
  if (field === 'commerce_subcategory') return text.kind ?? cell.value
  if (field === 'main_color') return text.color ?? cell.value
  if (field === 'styles') return text.styles.length ? text.styles.join(words.separator) : cell.value
  return cell.value
}

export function ComparisonTable({
  comparison,
  labels,
}: {
  comparison: ProductComparisonResult
  /** The reply's words (`ChatResponse.labels`) - English without them. */
  labels?: CardLabels | null
}) {
  const { products } = comparison
  const all = cardWords(labels)
  const words = all.comparison
  // A row no product's listing fills tells the customer nothing, so it is
  // left out. A row only some fill stays: the known value is still worth
  // comparing, with a dash where the other listing is silent.
  const rows = comparison.rows.filter((row) => row.cells.some((cell) => cell.known))
  // The stored columns, for each product none of whose measurements was read:
  // a sofa's `length` column is its width, so listing both for one product
  // would contradict the rows the backend read correctly. A product whose
  // measurements are in the rows gets a dash here; the row goes once every
  // product's are.
  const measured = products.map((_, i) =>
    rows.some((row) => SIZE_FIELDS.has(row.field) && row.cells[i]?.known),
  )
  const dimensions = measured.every(Boolean)
    ? null
    : products.map((product, i) => (measured[i] ? undefined : productText(product).size))
  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      <div className="border-b border-line bg-canvas/60 px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted">
        {words.title}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <th className="sticky left-0 z-10 bg-surface px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-muted">
                {words.field}
              </th>
              {products.map((p, i) => (
                <th
                  key={p.grounding_ref}
                  className="min-w-[10rem] px-4 py-3 text-left align-bottom font-medium text-ink"
                >
                  {p.image_url && (
                    <img
                      src={p.image_url}
                      alt=""
                      loading="lazy"
                      className="mb-2 aspect-[4/3] w-full max-w-[11rem] rounded-lg bg-canvas object-cover"
                    />
                  )}
                  <span className="mr-1 text-[11px] font-normal text-muted">
                    #{p.presented_ordinal ?? i + 1}
                  </span>
                  <br />
                  <span className="text-sm leading-snug" dir="auto">
                    {productText(p).name}
                  </span>
                  <div className="mt-1 text-sm font-semibold text-clay">
                    {productText(p).price}
                  </div>
                  {p.product_url && (
                    <a
                      href={p.product_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[11px] font-medium text-clay hover:underline"
                    >
                      {words.viewProduct}
                    </a>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {dimensions && (
              <tr className="border-t border-line">
                <td className="sticky left-0 z-10 bg-surface px-4 py-3 align-top">
                  <span className="font-medium text-ink">{words.listedDimensions}</span>
                  <p className="mt-1 max-w-48 text-xs text-muted">{words.listedCaveat}</p>
                </td>
                {dimensions.map((value, i) => (
                  <td
                    key={products[i].grounding_ref}
                    className={`px-4 py-3 align-top ${value ? 'text-ink' : 'text-muted/60'}`}
                  >
                    {value === undefined ? '—' : (value ?? words.notListed)}
                  </td>
                ))}
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.field} className="border-t border-line">
                <td className="sticky left-0 z-10 bg-surface px-4 py-3 align-top">
                  <span className="font-medium capitalize text-ink">
                    {words.fieldName(row.field) ?? humanise(row.field)}
                  </span>
                  {row.status !== 'unknown' && (
                    <span
                      className={`ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLES[row.status] ?? ''}`}
                    >
                      {words.status[row.status] ?? row.status}
                    </span>
                  )}
                </td>
                {row.cells.map((cell, i) => (
                  <td
                    key={i}
                    className={`px-4 py-3 align-top ${cell.known ? 'text-ink' : 'text-muted/60'}`}
                    title={cell.known ? undefined : words.notListedTitle}
                  >
                    {cellText(row.field, cell, products[i], all)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
