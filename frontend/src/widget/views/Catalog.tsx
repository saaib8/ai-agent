import { useEffect, useState } from 'react'
import { getCatalogProducts } from '../../api/client'
import type { CatalogItem } from '../../api/types'
import { dimensionsLine, humanise, money } from '../../lib/format'
import { ProductImage } from '../components/ProductCard'
import { useWidget } from '../context'
import { Icon } from '../icons'

const PAGE_SIZE = 12

interface State {
  items: CatalogItem[]
  page: number
  totalPages: number
  count: number
  loading: boolean
  error: string | null
}

const EMPTY: State = { items: [], page: 0, totalPages: 0, count: 0, loading: true, error: null }

/** The store's own catalogue, straight from the store-scoped browse API. */
export function Catalog() {
  const { config, facets } = useWidget()
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [category, setCategory] = useState<string | null>(null)
  const [state, setState] = useState<State>(EMPTY)
  const [page, setPage] = useState(1)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [query])

  // A new filter starts from page one.
  useEffect(() => setPage(1), [debounced, category])

  useEffect(() => {
    const controller = new AbortController()
    setState((current) => ({ ...(page === 1 ? EMPTY : current), loading: true, error: null }))
    void getCatalogProducts(
      config.apiBase,
      config.storeId,
      { q: debounced || undefined, category: category ?? undefined, page, page_size: PAGE_SIZE, sort: 'featured' },
      controller.signal,
    ).then((result) => {
      if (controller.signal.aborted) return
      if (!result.ok) {
        setState((current) => ({ ...current, loading: false, error: "The catalogue couldn't be loaded. Please try again." }))
        return
      }
      setState((current) => ({
        items: page === 1 ? result.data.items : [...current.items, ...result.data.items],
        page: result.data.page,
        totalPages: result.data.total_pages,
        count: result.data.count,
        loading: false,
        error: null,
      }))
    })
    return () => controller.abort()
  }, [config.apiBase, config.storeId, debounced, category, page])

  const categories = (facets?.categories ?? []).filter((c) => c.count > 0).sort((a, b) => b.count - a.count).slice(0, 8)

  return (
    <div className="scroll-inner">
      <div className="catalog">
        <div className="eyebrow">The {config.storeName} collection</div>
        <h2>Find your next piece.</h2>
        <div className="search">
          <Icon name="search" size={16} />
          <input
            className="input"
            placeholder="Search pieces by name"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search the catalogue"
          />
        </div>
        <div className="filter-row" role="group" aria-label="Categories">
          <button className="chip" aria-pressed={category === null} onClick={() => setCategory(null)}>
            All
          </button>
          {categories.map((c) => (
            <button key={c.value} className="chip" aria-pressed={category === c.value} onClick={() => setCategory(c.value)} style={{ textTransform: 'capitalize' }}>
              {humanise(c.value)}
            </button>
          ))}
        </div>

        {state.error && <div className="bubble-error">{state.error}</div>}

        {!state.loading && !state.error && state.items.length === 0 && (
          <p className="note">Nothing matches that yet — try another word or category.</p>
        )}

        <div className="grid">
          {state.items.map((item) => (
            <CatalogCard key={item.product_id} item={item} />
          ))}
        </div>

        <div className="center">
          {state.loading ? (
            <span className="muted" role="status">
              Loading…
            </span>
          ) : state.page < state.totalPages ? (
            <button className="link-btn" onClick={() => setPage((p) => p + 1)}>
              Show more ({state.count - state.items.length} left)
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function CatalogCard({ item }: { item: CatalogItem }) {
  const { config, ask, agent } = useWidget()
  const kind = item.commerce.subcategory ?? item.commerce.category
  const dims = dimensionsLine(item.dimensions)
  const similar = () => {
    const colour = item.main_color ? `${item.main_color.toLowerCase()} ` : ''
    const type = kind ? humanise(kind) : 'pieces'
    ask(`Show me ${colour}${type} options similar to the ${item.name_english}`)
  }
  return (
    <article className="pcard">
      <a className="pimg" href={item.product_url || undefined} target="_blank" rel="noopener noreferrer" aria-label={`${item.name_english} — open in the store`}>
        <ProductImage src={item.image_url} alt="" />
      </a>
      <div className="pbody">
        {kind && <span className="pcat">{humanise(kind)}</span>}
        <a className="pname" href={item.product_url || undefined} target="_blank" rel="noopener noreferrer">
          {item.name_english}
        </a>
        {dims && <span className="pdims">{dims}</span>}
        <div className="pfoot">
          <span className="price">{money(item.price_amount, item.price_unit)}</span>
        </div>
        <button className="link-btn" style={{ alignSelf: 'stretch', justifyContent: 'center' }} onClick={similar} disabled={agent.sending}>
          <Icon name="spark" size={14} /> Ask {config.assistantName}
        </button>
      </div>
    </article>
  )
}
