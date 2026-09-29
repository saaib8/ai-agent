import type { ProductComparisonResult } from '../../api/types'
import { humanise, money } from '../../lib/format'

const STATUS_STYLES: Record<string, string> = {
  different: 'bg-clay-soft text-clay',
  same: 'bg-sage/12 text-sage',
}

export function ComparisonTable({ comparison }: { comparison: ProductComparisonResult }) {
  const { products } = comparison
  // A row no product's listing fills tells the customer nothing, so it is
  // left out. A row only some fill stays: the known value is still worth
  // comparing, with a dash where the other listing is silent.
  const rows = comparison.rows.filter((row) => row.cells.some((cell) => cell.known))
  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      <div className="border-b border-line bg-canvas/60 px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted">
        Comparison
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <th className="sticky left-0 z-10 bg-surface px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-muted">
                Field
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
                  <span className="text-sm leading-snug">{p.name_english}</span>
                  <div className="mt-1 text-sm font-semibold text-clay">
                    {money(p.price_amount, p.price_unit)}
                  </div>
                  {p.product_url && (
                    <a
                      href={p.product_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[11px] font-medium text-clay hover:underline"
                    >
                      View product →
                    </a>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.field} className="border-t border-line">
                <td className="sticky left-0 z-10 bg-surface px-4 py-3 align-top">
                  <span className="font-medium capitalize text-ink">{humanise(row.field)}</span>
                  {row.status !== 'unknown' && (
                    <span
                      className={`ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLES[row.status] ?? ''}`}
                    >
                      {row.status}
                    </span>
                  )}
                </td>
                {row.cells.map((cell, i) => (
                  <td
                    key={i}
                    className={`px-4 py-3 align-top ${cell.known ? 'text-ink' : 'text-muted/60'}`}
                    title={cell.known ? undefined : 'Not listed for this product'}
                  >
                    {cell.known ? cell.value : '—'}
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
