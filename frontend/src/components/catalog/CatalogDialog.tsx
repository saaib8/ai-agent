import { useEffect, useState } from 'react'
import { getCatalogFacets } from '../../api/client'
import type {
  CatalogFacets,
  CatalogItem,
  CatalogSelection,
  ErrorBody,
  RenderRoomSpec,
  RenderView,
} from '../../api/types'
import type { RoomDraft, SelectedPiece } from '../../lib/catalog'
import { parseSide, selectionTotal, unitCount } from '../../lib/catalog'
import { ChevronLeftIcon, CloseIcon, GridIcon, ImageIcon } from '../icons'
import { BrowseStep } from './BrowseStep'
import { RoomStep } from './RoomStep'

export type CatalogStep = 'browse' | 'room'

interface CatalogDialogProps {
  apiBase: string
  storeId: number
  selection: SelectedPiece[]
  onSelectionChange: (next: SelectedPiece[]) => void
  room: RoomDraft
  onRoomChange: (room: RoomDraft) => void
  initialStep: CatalogStep
  /** A turn is in flight: rendering waits for it. */
  busy: boolean
  onClose: () => void
  onVisualize: (items: CatalogSelection['items'], room: RenderRoomSpec, view: RenderView) => void
}

/**
 * Browse Catalogue: pick products and how many of each, describe the room,
 * then render it. Mounted only while open; the selection and the room live
 * with the caller so they survive closing it.
 */
export function CatalogDialog({
  apiBase,
  storeId,
  selection,
  onSelectionChange,
  room,
  onRoomChange,
  initialStep,
  busy,
  onClose,
  onVisualize,
}: CatalogDialogProps) {
  const [step, setStep] = useState<CatalogStep>(initialStep)
  const [facets, setFacets] = useState<CatalogFacets | null>(null)
  const [facetsError, setFacetsError] = useState<ErrorBody | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    setFacetsError(null)
    void getCatalogFacets(apiBase, storeId).then((result) => {
      if (cancelled) return
      if (result.ok) setFacets(result.data)
      else setFacetsError(result.error)
    })
    return () => {
      cancelled = true
    }
  }, [apiBase, storeId, attempt])

  useEffect(() => {
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  // A room with no approved style yet takes the one this store stocks most.
  useEffect(() => {
    if (!facets || facets.studio.styles.includes(room.style)) return
    const style = facets.styles[0]?.value ?? facets.studio.styles[0]
    if (style) onRoomChange({ ...room, style })
  }, [facets, room, onRoomChange])

  // An empty selection has no room to set up.
  useEffect(() => {
    if (selection.length === 0) setStep('browse')
  }, [selection.length])

  const add = (item: CatalogItem) => {
    if (selection.some((p) => p.item.product_id === item.product_id)) return
    onSelectionChange([...selection, { item, quantity: 1 }])
  }
  const setQuantity = (productId: number, quantity: number) =>
    onSelectionChange(
      quantity <= 0
        ? selection.filter((p) => p.item.product_id !== productId)
        : selection.map((p) => (p.item.product_id === productId ? { ...p, quantity } : p)),
    )

  const studio = facets?.studio
  const length = studio ? parseSide(room.length, studio.min_room_side_m, studio.max_room_side_m) : null
  const width = studio ? parseSide(room.width, studio.min_room_side_m, studio.max_room_side_m) : null
  const canRender =
    !!studio?.render_available &&
    selection.length > 0 &&
    length !== null &&
    width !== null &&
    studio.styles.includes(room.style) &&
    !busy

  const visualize = () => {
    if (!canRender || length === null || width === null) return
    onVisualize(
      selection.map((p) => ({ product_id: p.item.product_id, quantity: p.quantity })),
      { room_type: room.roomType, style: room.style, length_m: length, width_m: width },
      room.view,
    )
  }

  const units = unitCount(selection)
  const total = selectionTotal(selection)

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-4">
      <button
        type="button"
        aria-label="Close catalogue"
        tabIndex={-1}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-ink/40"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="catalog-title"
        className="relative z-10 flex h-[94dvh] w-full max-w-5xl animate-rise flex-col overflow-hidden rounded-t-2xl border border-line bg-canvas shadow-soft sm:h-[90dvh] sm:rounded-2xl"
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 sm:px-5">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-clay-soft/60 text-clay">
              <GridIcon size={18} />
            </span>
            <div className="min-w-0">
              <h2 id="catalog-title" className="text-[15px] font-semibold text-ink">
                Browse catalogue
              </h2>
              <ol className="flex gap-2 text-xs text-muted">
                <StepLabel n={1} active={step === 'browse'}>
                  Pick products
                </StepLabel>
                <StepLabel n={2} active={step === 'room'}>
                  Set up the room
                </StepLabel>
              </ol>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close catalogue"
            className="flex h-9 w-9 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30"
          >
            <CloseIcon size={18} />
          </button>
        </header>

        {facetsError ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
            <p className="max-w-sm text-sm text-muted">{facetsError.message}</p>
            <button
              type="button"
              onClick={() => setAttempt((a) => a + 1)}
              className="rounded-full border border-line px-4 py-1.5 text-sm transition hover:border-clay/40 hover:text-clay"
            >
              Try again
            </button>
          </div>
        ) : !facets ? (
          <div className="flex flex-1 items-center justify-center text-sm text-muted">Loading catalogue…</div>
        ) : (
          <>
            {/* Kept mounted while setting up the room, so filters and page survive. */}
            <div className={step === 'browse' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
              <BrowseStep
                apiBase={apiBase}
                storeId={storeId}
                facets={facets}
                selection={selection}
                onAdd={add}
                onQuantity={setQuantity}
              />
            </div>
            {step === 'room' && (
              <RoomStep
                studio={facets.studio}
                room={room}
                onRoomChange={onRoomChange}
                selection={selection}
                onQuantity={setQuantity}
              />
            )}
          </>
        )}

        <footer className="shrink-0 border-t border-line bg-surface px-4 py-3 sm:px-5">
          {step === 'browse' && selection.length > 0 && (
            <div className="mb-2.5 flex gap-2 overflow-x-auto [scrollbar-width:none]">
              {selection.map(({ item, quantity }) => (
                <div
                  key={item.product_id}
                  className="relative h-12 w-12 shrink-0 overflow-hidden rounded-lg border border-line bg-canvas"
                  title={item.name_english}
                >
                  <img src={item.image_url} alt="" className="h-full w-full object-cover" />
                  {quantity > 1 && (
                    <span className="absolute bottom-0 left-0 rounded-tr-md bg-ink/75 px-1 text-[10px] font-semibold text-white">
                      ×{quantity}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => setQuantity(item.product_id, 0)}
                    aria-label={`Remove ${item.name_english}`}
                    className="absolute right-0 top-0 flex h-4 w-4 items-center justify-center rounded-bl-md bg-ink/70 text-white hover:bg-ink"
                  >
                    <CloseIcon size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-center justify-between gap-3">
            {step === 'room' ? (
              <button
                type="button"
                onClick={() => setStep('browse')}
                className="inline-flex items-center gap-1 rounded-full border border-line px-3 py-2 text-sm text-ink transition hover:border-line-strong"
              >
                <ChevronLeftIcon size={16} />
                <span className="hidden sm:inline">Products</span>
              </button>
            ) : null}
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-ink">
                {selection.length === 0
                  ? 'Nothing picked yet'
                  : `${selection.length} ${selection.length === 1 ? 'product' : 'products'} · ${units} ${
                      units === 1 ? 'piece' : 'pieces'
                    }`}
                {studio && selection.length > 0 && (
                  <span className="font-normal text-muted"> (up to {studio.max_products})</span>
                )}
              </p>
              {total && <p className="text-xs text-muted">Total {total}</p>}
            </div>
            {step === 'browse' ? (
              <button
                type="button"
                onClick={() => setStep('room')}
                disabled={selection.length === 0 || !facets}
                className="inline-flex shrink-0 items-center gap-2 rounded-full bg-clay px-4 py-2 text-sm font-medium text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:bg-line-strong"
              >
                Set up the room
              </button>
            ) : (
              <button
                type="button"
                onClick={visualize}
                disabled={!canRender}
                title={
                  studio && !studio.render_available
                    ? 'Room visualisation is not configured'
                    : busy
                      ? 'Waiting for the current reply'
                      : undefined
                }
                className="inline-flex shrink-0 items-center gap-2 rounded-full bg-clay px-4 py-2 text-sm font-medium text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:bg-line-strong"
              >
                <ImageIcon size={16} />
                Visualize room
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  )
}

function StepLabel({ n, active, children }: { n: number; active: boolean; children: string }) {
  return (
    <li className={active ? 'font-medium text-clay' : ''}>
      {n}. {children}
    </li>
  )
}
