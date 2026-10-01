import { useEffect, useRef, useState } from 'react'
import { getCatalogProducts } from '../../api/client'
import { RENDER_VIEWS } from '../../api/types'
import type { CatalogItem, CatalogSort, StudioOptions } from '../../api/types'
import { ROOM_PRESETS, fitCheck, parseSide, selectionTotal, styleLabel, unitCount } from '../../lib/catalog'
import { dimensionsLine, humanise, money } from '../../lib/format'
import { ProductImage } from '../components/ProductCard'
import { useWidget } from '../context'
import { Icon } from '../icons'

const PAGE_SIZE = 12

const SORTS: { value: CatalogSort; label: string }[] = [
  { value: 'featured', label: 'Featured' },
  { value: 'price_asc', label: 'Price: low to high' },
  { value: 'price_desc', label: 'Price: high to low' },
]

interface State {
  items: CatalogItem[]
  page: number
  totalPages: number
  count: number
  loading: boolean
  error: string | null
}

const EMPTY: State = { items: [], page: 0, totalPages: 0, count: 0, loading: true, error: null }

/**
 * Browse Catalogue, as in the agent console: pick pieces and how many of
 * each, then set up the room and render it. The picks wait in a tray at the
 * bottom with their count and total.
 */
export function Catalog() {
  const { selection, setSelection, facets } = useWidget()
  const [step, setStep] = useState<'browse' | 'room'>('browse')
  const studio = facets?.studio

  // An empty selection has no room to set up.
  useEffect(() => {
    if (selection.length === 0) setStep('browse')
  }, [selection.length])

  const setQuantity = (productId: number, quantity: number) =>
    setSelection(
      quantity <= 0
        ? selection.filter((p) => p.item.product_id !== productId)
        : selection.map((p) => (p.item.product_id === productId ? { ...p, quantity } : p)),
    )

  return (
    <div className="scroll-inner catalog-screen">
      <div className="catalog-steps" aria-label="Steps">
        <span className={step === 'browse' ? 'on' : undefined}>1. Pick products</span>
        <span className={step === 'room' ? 'on' : undefined}>2. Set up the room</span>
      </div>
      {/* Kept mounted while setting up the room, so filters and pages survive. */}
      <div hidden={step !== 'browse'}>
        <Browse setQuantity={setQuantity} />
      </div>
      {step === 'room' && studio && <RoomSetup studio={studio} setQuantity={setQuantity} />}
      <SelectionTray step={step} setStep={setStep} setQuantity={setQuantity} />
    </div>
  )
}

type SetQuantity = (productId: number, quantity: number) => void

function Browse({ setQuantity }: { setQuantity: SetQuantity }) {
  const { config, facets, selection, setSelection, agent } = useWidget()
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [category, setCategory] = useState('')
  const [color, setColor] = useState('')
  const [style, setStyle] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [sort, setSort] = useState<CatalogSort>('featured')
  const [state, setState] = useState<State>(EMPTY)
  const [page, setPage] = useState(1)
  const currency = facets?.price?.currency

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [query])

  // A new filter starts from page one.
  useEffect(() => setPage(1), [debounced, category, color, style, minPrice, maxPrice, sort])

  useEffect(() => {
    const controller = new AbortController()
    setState((current) => ({ ...(page === 1 ? EMPTY : current), loading: true, error: null }))
    void getCatalogProducts(
      config.apiBase,
      config.storeId,
      {
        q: debounced || undefined,
        category: category || undefined,
        color: color || undefined,
        style: style || undefined,
        // Bounds travel with the currency they are in, or not at all.
        ...(currency && (minPrice || maxPrice)
          ? { min_price: minPrice || undefined, max_price: maxPrice || undefined, currency }
          : {}),
        page,
        page_size: PAGE_SIZE,
        sort,
      },
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
  }, [config.apiBase, config.storeId, debounced, category, color, style, minPrice, maxPrice, currency, sort, page])

  const quantities = new Map(selection.map((p) => [p.item.product_id, p.quantity]))
  const maxProducts = facets?.studio.max_products ?? Infinity
  const maxQuantity = facets?.studio.max_quantity ?? 9
  const atCap = selection.length >= maxProducts

  const add = (item: CatalogItem) => {
    if (quantities.has(item.product_id)) return
    if (atCap) {
      agent.flash(`Up to ${maxProducts} products in one room`)
      return
    }
    setSelection([...selection, { item, quantity: 1 }])
  }

  return (
    <div className="catalog">
      <div className="catalog-search">
        <div className="search">
          <Icon name="search" size={16} />
          <input
            className="input"
            placeholder="Search by name…"
            value={query}
            maxLength={80}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search the catalogue"
          />
        </div>
        <select className="select" value={sort} onChange={(e) => setSort(e.target.value as CatalogSort)} aria-label="Sort">
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div className="catalog-filters">
        <select className="select" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
          <option value="">All categories</option>
          {(facets?.categories ?? []).filter((c) => c.count > 0).map((c) => (
            <option key={c.value} value={c.value}>
              {humanise(c.value)}
            </option>
          ))}
        </select>
        <select className="select" value={color} onChange={(e) => setColor(e.target.value)} aria-label="Colour">
          <option value="">Any colour</option>
          {(facets?.colors ?? []).map((c) => (
            <option key={c.value} value={c.value}>
              {humanise(c.value)}
            </option>
          ))}
        </select>
        <select className="select" value={style} onChange={(e) => setStyle(e.target.value)} aria-label="Style">
          <option value="">Any style</option>
          {(facets?.styles ?? []).map((s) => (
            <option key={s.value} value={s.value}>
              {styleLabel(s.value)}
            </option>
          ))}
        </select>
        <div className="price-range">
          <input className="select" inputMode="numeric" placeholder="Min" value={minPrice} onChange={(e) => setMinPrice(e.target.value.replace(/\D/g, ''))} aria-label="Minimum price" />
          <span aria-hidden="true">–</span>
          <input className="select" inputMode="numeric" placeholder="Max" value={maxPrice} onChange={(e) => setMaxPrice(e.target.value.replace(/\D/g, ''))} aria-label="Maximum price" />
          {currency && <span className="muted">{currency}</span>}
        </div>
      </div>

      {state.error && <div className="bubble-error">{state.error}</div>}

      {!state.loading && !state.error && state.items.length === 0 && (
        <p className="note">Nothing matches that yet — try another word or filter.</p>
      )}

      <div className="grid">
        {state.items.map((item) => (
          <CatalogCard
            key={item.product_id}
            item={item}
            quantity={quantities.get(item.product_id) ?? 0}
            maxQuantity={maxQuantity}
            onAdd={() => add(item)}
            onQuantity={(n) => setQuantity(item.product_id, n)}
          />
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
  )
}

function CatalogCard({
  item,
  quantity,
  maxQuantity,
  onAdd,
  onQuantity,
}: {
  item: CatalogItem
  quantity: number
  maxQuantity: number
  onAdd: () => void
  onQuantity: (n: number) => void
}) {
  const kind = item.commerce.subcategory ?? item.commerce.category
  const dims = dimensionsLine(item.dimensions)
  return (
    <article className={`pcard${quantity > 0 ? ' selected' : ''}`}>
      <a className="pimg" href={item.product_url || undefined} target="_blank" rel="noopener noreferrer" aria-label={`${item.name_english} — open in the store`}>
        <ProductImage src={item.image_url} alt="" />
      </a>
      <div className="pbody">
        <a className="pname" href={item.product_url || undefined} target="_blank" rel="noopener noreferrer">
          {item.name_english}
        </a>
        <span className="price accent-price">{money(item.price_amount, item.price_unit)}</span>
        {kind && <span className="pdims pkind">{humanise(kind)}</span>}
        {dims && <span className="pdims">{dims}</span>}
        <div className="cat-actions">
          {quantity > 0 ? (
            <Stepper value={quantity} max={maxQuantity} onChange={onQuantity} label={item.name_english} />
          ) : (
            <button className="add-pill" onClick={onAdd}>
              <Icon name="plus" size={14} strokeWidth={2} /> Add
            </button>
          )}
          {item.product_url && (
            <a className="view-link" href={item.product_url} target="_blank" rel="noopener noreferrer">
              View
            </a>
          )}
        </div>
      </div>
    </article>
  )
}

/** − n + for one product's quantity. Stepping below one removes it. */
function Stepper({ value, max, onChange, label }: { value: number; max: number; onChange: (n: number) => void; label: string }) {
  return (
    <div className="stepper" role="group" aria-label={`Quantity of ${label}`}>
      <button onClick={() => onChange(value - 1)} aria-label={value === 1 ? `Remove ${label}` : `One fewer ${label}`}>
        <Icon name="minus" size={14} strokeWidth={2} />
      </button>
      <span aria-live="polite">{value}</span>
      <button
        onClick={() => onChange(value + 1)}
        disabled={value >= max}
        aria-label={`One more ${label}`}
        title={value >= max ? `At most ${max} of one product` : undefined}
      >
        <Icon name="plus" size={14} strokeWidth={2} />
      </button>
    </div>
  )
}

/** Describe the room, check the pieces fit it, choose the camera. */
function RoomSetup({ studio, setQuantity }: { studio: StudioOptions; setQuantity: SetQuantity }) {
  const { room, setRoom, selection, facets } = useWidget()
  const set = (patch: Partial<typeof room>) => setRoom({ ...room, ...patch })
  const min = studio.min_room_side_m
  const max = studio.max_room_side_m
  const length = parseSide(room.length, min, max)
  const width = parseSide(room.width, min, max)
  const sizeError = (room.length && length === null) || (room.width && width === null) ? `Enter sides between ${min} and ${max} m.` : null
  const fit = length !== null && width !== null ? fitCheck(selection, length, width, studio.crowded_floor_ratio) : null

  // A room with no approved style yet takes the one this store stocks most.
  useEffect(() => {
    if (studio.styles.includes(room.style)) return
    const style = facets?.styles.find((s) => studio.styles.includes(s.value))?.value ?? studio.styles[0]
    if (style) setRoom({ ...room, style })
  }, [studio, facets, room, setRoom])

  return (
    <div className="room-setup">
      <Field label="Room">
        <div className="chips-wrap" role="radiogroup" aria-label="Room type">
          {studio.room_types.map((option) => (
            <button key={option.value} className="chip" role="radio" aria-checked={room.roomType === option.value} aria-pressed={room.roomType === option.value} onClick={() => set({ roomType: option.value })}>
              {option.label}
            </button>
          ))}
        </div>
      </Field>
      <Field label="Style">
        <StyleRail styles={studio.styles} value={room.style} onChange={(style) => set({ style })} />
      </Field>
      <Field label="Size">
        <div className="size-row">
          <label className="side-box">
            <input type="number" inputMode="decimal" min={min} max={max} step={0.1} value={room.length} onChange={(e) => set({ length: e.target.value })} aria-label="Room length in metres" />
            <span>m</span>
          </label>
          <span className="muted" aria-hidden="true">×</span>
          <label className="side-box">
            <input type="number" inputMode="decimal" min={min} max={max} step={0.1} value={room.width} onChange={(e) => set({ width: e.target.value })} aria-label="Room width in metres" />
            <span>m</span>
          </label>
        </div>
        <div className="chips-wrap">
          {ROOM_PRESETS.map(([l, w]) => (
            <button key={`${l}x${w}`} className="chip" aria-pressed={room.length === String(l) && room.width === String(w)} onClick={() => set({ length: String(l), width: String(w) })}>
              {l} × {w} m
            </button>
          ))}
        </div>
        {sizeError && <p className="field-error">{sizeError}</p>}
      </Field>
      <Field label="View">
        <div className="segmented" role="radiogroup" aria-label="Camera view">
          {RENDER_VIEWS.map((v) => (
            <button key={v.value} role="radio" aria-checked={room.view === v.value} onClick={() => set({ view: v.value })}>
              {v.label}
            </button>
          ))}
        </div>
      </Field>

      {fit ? (
        <div className={`fit-card ${fit.level}`}>
          <div className="fit-head">
            <strong>{fit.level === 'over' ? 'Too much for this room' : fit.level === 'crowded' ? 'This will feel crowded' : 'Fits comfortably'}</strong>
            <span className="muted">
              {fit.coveredM2.toFixed(1)} of {fit.floorM2.toFixed(1)} m² · {Math.round(fit.ratio * 100)}%
            </span>
          </div>
          <div className="fit-bar">
            <i style={{ width: `${Math.min(100, Math.round(fit.ratio * 100))}%` }} />
          </div>
          {fit.tooBig.map((piece) => (
            <p key={piece.name} className="field-error">
              {piece.name} ({piece.lengthM} m) won&apos;t fit this room.
            </p>
          ))}
          <p className="muted small">Rugs, lighting and decor aren&apos;t counted.</p>
        </div>
      ) : (
        <div className="fit-card">Enter the room size to check the pieces fit.</div>
      )}

      <Field label="In the room">
        <ul className="room-list">
          {selection.map(({ item, quantity }) => (
            <li key={item.product_id}>
              <img src={item.image_url} alt="" />
              <div className="room-item">
                <span className="tray-name" title={item.name_english}>
                  {item.name_english}
                </span>
                <span className="muted small">{money(item.price_amount, item.price_unit)}</span>
              </div>
              <Stepper value={quantity} max={studio.max_quantity} onChange={(n) => setQuantity(item.product_id, n)} label={item.name_english} />
            </li>
          ))}
        </ul>
      </Field>
    </div>
  )
}

/** Every approved style as one scrolling row of chips; the chosen one is
 *  brought into view, so it is never hidden off the edge. */
function StyleRail({ styles, value, onChange }: { styles: string[]; value: string; onChange: (style: string) => void }) {
  const rail = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = rail.current
    const chip = el?.querySelector<HTMLElement>('[aria-checked="true"]')
    if (!el || !chip) return
    const offset = chip.getBoundingClientRect().left - el.getBoundingClientRect().left
    el.scrollTo({ left: el.scrollLeft + offset - 16, behavior: 'instant' })
  }, [value])
  return (
    <div className="style-rail" ref={rail} role="radiogroup" aria-label="Room style">
      {styles.map((style) => (
        <button key={style} className="chip" role="radio" aria-checked={value === style} aria-pressed={value === style} onClick={() => onChange(style)}>
          {styleLabel(style)}
        </button>
      ))}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      {children}
    </div>
  )
}

/** Thumbnails of the picks, their count and total, and the next step. */
function SelectionTray({
  step,
  setStep,
  setQuantity,
}: {
  step: 'browse' | 'room'
  setStep: (step: 'browse' | 'room') => void
  setQuantity: SetQuantity
}) {
  const { selection, room, facets, agent, go } = useWidget()
  const scroller = useRef<HTMLDivElement>(null)
  const studio = facets?.studio
  const units = unitCount(selection)
  const total = selectionTotal(selection)
  const length = studio ? parseSide(room.length, studio.min_room_side_m, studio.max_room_side_m) : null
  const width = studio ? parseSide(room.width, studio.min_room_side_m, studio.max_room_side_m) : null
  const canRender =
    !!studio?.render_available && selection.length > 0 && length !== null && width !== null && studio.styles.includes(room.style) && !agent.sending

  const visualize = () => {
    if (!canRender || length === null || width === null) return
    const viewLabel = RENDER_VIEWS.find((v) => v.value === room.view)?.label ?? room.view
    agent.visualizeSelection(
      {
        items: selection.map((p) => ({ product_id: p.item.product_id, quantity: p.quantity })),
        room: { room_type: room.roomType, style: room.style, length_m: length, width_m: width },
      },
      room.view,
      `Visualize my selection — ${units} ${units === 1 ? 'piece' : 'pieces'} in a ${length} × ${width} m ` +
        `${styleLabel(room.style).toLowerCase()} ${humanise(room.roomType)}, ${viewLabel.toLowerCase()} view`,
    )
    go('chat')
  }

  return (
    <div className="selection-tray">
      {step === 'browse' && selection.length > 0 && (
        <div className="tray-thumbs" ref={scroller}>
          {selection.map(({ item, quantity }) => (
            <div key={item.product_id} className="tray-thumb-item" title={item.name_english}>
              <img src={item.image_url} alt="" />
              {quantity > 1 && <span className="qty">×{quantity}</span>}
              <button onClick={() => setQuantity(item.product_id, 0)} aria-label={`Remove ${item.name_english}`}>
                <Icon name="close" size={10} strokeWidth={2.4} />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="tray-row">
        {step === 'room' && (
          <button className="back-pill" onClick={() => setStep('browse')} aria-label="Back to products">
            <Icon name="back" size={16} />
          </button>
        )}
        <div className="tray-summary">
          <strong>
            {selection.length === 0
              ? 'Nothing picked yet'
              : `${selection.length} ${selection.length === 1 ? 'product' : 'products'} · ${units} ${units === 1 ? 'piece' : 'pieces'}`}
            {studio && selection.length > 0 && <span className="muted"> (up to {studio.max_products})</span>}
          </strong>
          {total && <span className="muted">Total {total}</span>}
        </div>
        {step === 'browse' ? (
          <button className="tray-go" onClick={() => setStep('room')} disabled={selection.length === 0 || !studio}>
            Set up the room
          </button>
        ) : (
          <button
            className="tray-go"
            onClick={visualize}
            disabled={!canRender}
            title={studio && !studio.render_available ? 'Room visualisation is not available' : undefined}
          >
            <Icon name="image" size={15} /> Visualize room
          </button>
        )}
      </div>
    </div>
  )
}
