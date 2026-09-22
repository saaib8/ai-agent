import type { ProductComparisonResult } from '../../api/types'
import { humanise } from '../../lib/format'

const STATUS_STYLES: Record<string, string> = {
  different: 'bg-clay-soft text-clay',
  same: 'bg-sage/12 text-sage',
  unknown: 'bg-canvas text-muted',
}

export function ComparisonTable({ comparison }: { comparison: ProductComparisonResult }) {
  const { products, rows } = comparison
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
                  className="min-w-[9rem] px-4 py-3 text-left align-bottom font-medium text-ink"
                >
                  <span className="mr-1 text-[11px] font-normal text-muted">
                    #{p.presented_ordinal ?? i + 1}
                  </span>
                  <br />
                  {p.name_english}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.field} className="border-t border-line">
                <td className="sticky left-0 z-10 bg-surface px-4 py-3 align-top">
                  <span className="font-medium capitalize text-ink">{humanise(row.field)}</span>
                  <span
                    className={`ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLES[row.status] ?? ''}`}
                  >
                    {row.status}
                  </span>
                </td>
                {row.cells.map((cell, i) => (
                  <td
                    key={i}
                    className={`px-4 py-3 align-top ${cell.known ? 'text-ink' : 'italic text-muted/70'}`}
                  >
                    {cell.known ? cell.value : 'unknown'}
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
