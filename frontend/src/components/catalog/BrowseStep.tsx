import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { getCatalogProducts } from '../../api/client'
import type { CatalogFacets, CatalogItem, CatalogPage, CatalogSort, ErrorBody } from '../../api/types'
import type { SelectedPiece } from '../../lib/catalog'
import { styleLabel } from '../../lib/catalog'
import { humanise } from '../../lib/format'
import { ChevronLeftIcon, ChevronRightIcon, CloseIcon, SearchIcon } from '../icons'
import { CatalogTile } from './CatalogTile'

interface BrowseStepProps {
  apiBase: string
  storeId: number
  facets: CatalogFacets
  selection: SelectedPiece[]
  onAdd: (item: CatalogItem) => void
  onQuantity: (productId: number, quantity: number) => void
}

const SORTS: { value: CatalogSort; label: string }[] = [
  { value: 'featured', label: 'Featured' },
  { value: 'price_asc', label: 'Price: low to high' },
  { value: 'price_desc', label: 'Price: high to low' },
]

const fieldClass =
  'h-9 rounded-full border border-line bg-surface px-3 text-sm text-ink transition focus:border-clay/50 focus:outline-none focus:ring-2 focus:ring-clay/15'

/** Search, filter and page through the store's catalog, adding pieces. */
export function BrowseStep({ apiBase, storeId, facets, selection, onAdd, onQuantity }: BrowseStepProps) {
  const [searchInput, setSearchInput] = useState('')
  const [q, setQ] = useState('')
  const [category, setCategory] = useState('')
  const [subcategory, setSubcategory] = useState('')
  const [color, setColor] = useState('')
  const [style, setStyle] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [sort, setSort] = useState<CatalogSort>('featured')
  const [page, setPage] = useState(1)
  const [attempt, setAttempt] = useState(0)

  const [data, setData] = useState<CatalogPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ErrorBody | null>(null)
  const results = useRef<HTMLDivElement>(null)

  // A new filter or page shows its first row, not wherever the last one was scrolled to.
  useEffect(() => {
    results.current?.scrollTo({ top: 0 })
  }, [q, category, subcategory, color, style, minPrice, maxPrice, sort, page])

  // Debounce typing into one request; a new search starts at page one.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(searchInput.trim())
      setPage(1)
    }, 300)
    return () => clearTimeout(t)
  }, [searchInput])

  const currency = facets.price?.currency
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    void getCatalogProducts(
      apiBase,
      storeId,
      {
        q: q || undefined,
        category: category || undefined,
        subcategory: subcategory || undefined,
        color: color || undefined,
        style: style || undefined,
        // Bounds travel with the currency they are in, or not at all.
        ...(currency && (minPrice || maxPrice)
          ? { min_price: minPrice || undefined, max_price: maxPrice || undefined, currency }
          : {}),
        sort,
        page,
      },
      controller.signal,
    ).then((result) => {
      if (controller.signal.aborted) return
      if (result.ok) setData(result.data)
      else setError(result.error)
      setLoading(false)
    })
    return () => controller.abort()
  }, [apiBase, storeId, q, category, subcategory, color, style, minPrice, maxPrice, currency, sort, page, attempt])

  const hasFilters = !!(q || category || color || style || minPrice || maxPrice)
  const clearFilters = () => {
    setSearchInput('')
    setQ('')
    setCategory('')
    setSubcategory('')
    setColor('')
    setStyle('')
    setMinPrice('')
    setMaxPrice('')
    setPage(1)
  }
  const onFilter = (set: (v: string) => void) => (value: string) => {
    set(value)
    setPage(1)
  }

  const subcategories = facets.categories.find((c) => c.value === category)?.subcategories ?? []
  const quantities = new Map(selection.map((p) => [p.item.product_id, p.quantity]))
  const atCap = selection.length >= facets.studio.max_products
  const priceHint = facets.price

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-2.5 border-b border-line px-4 py-3 sm:px-5">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <SearchIcon
              size={16}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
            />
            <input
              type="text"
              inputMode="search"
              enterKeyHint="search"
              dir="auto"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              maxLength={80}
              placeholder="Search by name…"
              aria-label="Search products"
              autoFocus
              className={`${fieldClass} w-full pl-9 pr-9`}
            />
            {searchInput && (
              <button
                type="button"
                onClick={() => setSearchInput('')}
                aria-label="Clear search"
                className="absolute right-2 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-full text-muted hover:bg-canvas hover:text-ink"
              >
                <CloseIcon size={14} />
              </button>
            )}
          </div>
          <Select
            label="Sort"
            value={sort}
            onChange={(v) => {
              setSort(v as CatalogSort)
              setPage(1)
            }}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Select>
        </div>

        {/* One swipeable row on a phone; wraps where there is room. */}
        <div className="-mx-4 flex items-center gap-2 overflow-x-auto px-4 [scrollbar-width:none] sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 [&>*]:shrink-0">
          <Select
            label="Category"
            value={category}
            onChange={(v) => {
              setCategory(v)
              setSubcategory('')
              setPage(1)
            }}
          >
            <option value="">All categories</option>
            {facets.categories.map((c) => (
              <option key={c.value} value={c.value}>
                {capitalise(humanise(c.value))} ({c.count})
              </option>
            ))}
          </Select>
          {category && subcategories.length > 0 && (
            <Select label="Type" value={subcategory} onChange={onFilter(setSubcategory)}>
              <option value="">All types</option>
              {subcategories.map((s) => (
                <option key={s.value} value={s.value}>
                  {capitalise(humanise(s.value))} ({s.count})
                </option>
              ))}
            </Select>
          )}
          {facets.colors.length > 0 && (
            <Select label="Colour" value={color} onChange={onFilter(setColor)}>
              <option value="">Any colour</option>
              {facets.colors.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.value} ({c.count})
                </option>
              ))}
            </Select>
          )}
          {facets.styles.length > 0 && (
            <Select label="Style" value={style} onChange={onFilter(setStyle)}>
              <option value="">Any style</option>
              {facets.styles.map((s) => (
                <option key={s.value} value={s.value}>
                  {styleLabel(s.value)} ({s.count})
                </option>
              ))}
            </Select>
          )}
          {priceHint && (
            <div className="flex items-center gap-1.5">
              <PriceInput
                label="Minimum price"
                value={minPrice}
                placeholder="Min"
                onChange={onFilter(setMinPrice)}
              />
              <span className="text-muted">–</span>
              <PriceInput
                label="Maximum price"
                value={maxPrice}
                placeholder="Max"
                onChange={onFilter(setMaxPrice)}
              />
              <span
                className="text-xs text-muted"
                title={`${Math.floor(Number(priceHint.min_amount))}–${Math.ceil(Number(priceHint.max_amount))} ${priceHint.currency}`}
              >
                {priceHint.currency}
              </span>
            </div>
          )}
          {hasFilters && (
            <button
              type="button"
              onClick={clearFilters}
              className="px-1 text-xs font-medium text-clay hover:underline"
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      <div ref={results} className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5" aria-busy={loading}>
        {error ? (
          <Centered>
            <p className="max-w-xs text-sm text-muted">{error.message}</p>
            <button
              type="button"
              onClick={() => setAttempt((a) => a + 1)}
              className="rounded-full border border-line px-4 py-1.5 text-sm transition hover:border-clay/40 hover:text-clay"
            >
              Try again
            </button>
          </Centered>
        ) : !data ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {Array.from({ length: 8 }, (_, i) => (
              <div key={i} className="h-64 animate-pulse rounded-2xl bg-surface-muted/70" />
            ))}
          </div>
        ) : data.items.length === 0 ? (
          <Centered>
            <p className="text-sm text-muted">No products match these filters.</p>
            {hasFilters && (
              <button
                type="button"
                onClick={clearFilters}
                className="rounded-full border border-line px-4 py-1.5 text-sm transition hover:border-clay/40 hover:text-clay"
              >
                Clear filters
              </button>
            )}
          </Centered>
        ) : (
          <>
            <div
              className={`grid grid-cols-2 gap-3 transition-opacity sm:grid-cols-3 lg:grid-cols-4 ${
                loading ? 'pointer-events-none opacity-60' : ''
              }`}
            >
              {data.items.map((item) => (
                <CatalogTile
                  key={item.product_id}
                  item={item}
                  quantity={quantities.get(item.product_id) ?? 0}
                  maxQuantity={facets.studio.max_quantity}
                  canAdd={!atCap}
                  maxProducts={facets.studio.max_products}
                  onAdd={() => onAdd(item)}
                  onQuantity={(n) => onQuantity(item.product_id, n)}
                />
              ))}
            </div>
            <div className="mt-4 flex items-center justify-between gap-2">
              <p className="text-xs text-muted">
                {data.count.toLocaleString()} products · page {data.page} of{' '}
                {Math.max(data.total_pages, 1)}
              </p>
              <div className="flex gap-1">
                <PagerButton
                  label="Previous page"
                  disabled={page <= 1 || loading}
                  onClick={() => setPage(Math.max(1, page - 1))}
                >
                  <ChevronLeftIcon size={16} />
                </PagerButton>
                <PagerButton
                  label="Next page"
                  disabled={page >= data.total_pages || loading}
                  onClick={() => setPage(page + 1)}
                >
                  <ChevronRightIcon size={16} />
                </PagerButton>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Select({
  label,
  value,
  onChange,
  children,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  children: ReactNode
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`${fieldClass} max-w-[12rem] cursor-pointer ${value ? 'border-clay/40 text-clay' : ''}`}
    >
      {children}
    </select>
  )
}

function PriceInput({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string
  value: string
  placeholder: string
  onChange: (value: string) => void
}) {
  return (
    <input
      type="number"
      inputMode="numeric"
      min={0}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={label}
      className={`${fieldClass} w-24`}
    />
  )
}

function PagerButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string
  disabled: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted transition hover:border-clay/40 hover:text-clay disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  )
}

function Centered({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-48 flex-col items-center justify-center gap-3 text-center">
      {children}
    </div>
  )
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}
