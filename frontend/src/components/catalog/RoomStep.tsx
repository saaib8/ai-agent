import type { ReactNode } from 'react'
import type { StudioOptions } from '../../api/types'
import type { FitCheck, RoomDraft, SelectedPiece } from '../../lib/catalog'
import { ROOM_PRESETS, fitCheck, parseSide, styleLabel } from '../../lib/catalog'
import { money } from '../../lib/format'
import { AlertIcon, CloseIcon } from '../icons'
import { ViewPicker } from '../presentation/ViewPicker'
import { QuantityStepper } from './QuantityStepper'

interface RoomStepProps {
  studio: StudioOptions
  room: RoomDraft
  onRoomChange: (room: RoomDraft) => void
  selection: SelectedPiece[]
  onQuantity: (productId: number, quantity: number) => void
}

const fieldClass =
  'h-10 rounded-xl border border-line bg-surface px-3 text-sm text-ink transition focus:border-clay/50 focus:outline-none focus:ring-2 focus:ring-clay/15'

const pillClass = (active: boolean) =>
  `rounded-full border px-3 py-1.5 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 ${
    active
      ? 'border-clay bg-clay text-white'
      : 'border-line bg-surface text-ink hover:border-clay/40 hover:text-clay'
  }`

/** Describe the room, check the pieces fit it, choose the camera. */
export function RoomStep({ studio, room, onRoomChange, selection, onQuantity }: RoomStepProps) {
  const set = (patch: Partial<RoomDraft>) => onRoomChange({ ...room, ...patch })
  const min = studio.min_room_side_m
  const max = studio.max_room_side_m
  const length = parseSide(room.length, min, max)
  const width = parseSide(room.width, min, max)
  const sizeError =
    (room.length && length === null) || (room.width && width === null)
      ? `Enter sides between ${min} and ${max} m.`
      : null
  const fit =
    length !== null && width !== null
      ? fitCheck(selection, length, width, studio.crowded_floor_ratio)
      : null

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-5">
          <Field label="Room">
            <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Room type">
              {studio.room_types.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={room.roomType === option.value}
                  onClick={() => set({ roomType: option.value })}
                  className={pillClass(room.roomType === option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Style" htmlFor="room-style">
            <select
              id="room-style"
              value={room.style}
              onChange={(e) => set({ style: e.target.value })}
              className={`${fieldClass} w-full max-w-xs cursor-pointer`}
            >
              {studio.styles.map((style) => (
                <option key={style} value={style}>
                  {styleLabel(style)}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Size (metres)">
            <div className="flex flex-wrap items-center gap-2">
              <SideInput label="Room length in metres" value={room.length} min={min} max={max} onChange={(v) => set({ length: v })} />
              <span className="text-muted" aria-hidden>
                ×
              </span>
              <SideInput label="Room width in metres" value={room.width} min={min} max={max} onChange={(v) => set({ width: v })} />
              <div className="flex flex-wrap gap-1.5">
                {ROOM_PRESETS.map(([l, w]) => (
                  <button
                    key={`${l}x${w}`}
                    type="button"
                    onClick={() => set({ length: String(l), width: String(w) })}
                    className={pillClass(room.length === String(l) && room.width === String(w))}
                  >
                    {l} × {w}
                  </button>
                ))}
              </div>
            </div>
            {sizeError && <p className="mt-1.5 text-xs text-rose">{sizeError}</p>}
          </Field>

          <Field label="View">
            <ViewPicker value={room.view} onChange={(view) => set({ view })} />
          </Field>
        </div>

        <div className="space-y-4">
          <FitCard fit={fit} ratio={studio.crowded_floor_ratio} />

          <div>
            <h3 className="mb-2 text-sm font-semibold text-ink">In the room</h3>
            <ul className="divide-y divide-line rounded-2xl border border-line bg-surface">
              {selection.map(({ item, quantity }) => (
                <li key={item.product_id} className="flex items-center gap-3 px-3 py-2.5">
                  <img
                    src={item.image_url}
                    alt=""
                    className="h-11 w-11 shrink-0 rounded-lg bg-canvas object-cover"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm text-ink" title={item.name_english}>
                      {item.name_english}
                    </div>
                    <div className="text-xs text-muted">{money(item.price_amount, item.price_unit)}</div>
                  </div>
                  <QuantityStepper
                    value={quantity}
                    max={studio.max_quantity}
                    onChange={(n) => onQuantity(item.product_id, n)}
                    label={item.name_english}
                    size="sm"
                  />
                  <button
                    type="button"
                    onClick={() => onQuantity(item.product_id, 0)}
                    aria-label={`Remove ${item.name_english}`}
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-rose"
                  >
                    <CloseIcon size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}

const LEVEL_META: Record<FitCheck['level'], { bar: string; text: string }> = {
  ok: { bar: 'bg-sage', text: 'text-sage' },
  crowded: { bar: 'bg-amber', text: 'text-amber' },
  over: { bar: 'bg-rose', text: 'text-rose' },
}

function FitCard({ fit, ratio }: { fit: FitCheck | null; ratio: number }) {
  if (!fit) {
    return (
      <div className="rounded-2xl border border-line bg-canvas/60 px-4 py-3 text-sm text-muted">
        Enter the room size to check the pieces fit.
      </div>
    )
  }
  const meta = LEVEL_META[fit.level]
  const pct = Math.round(fit.ratio * 100)
  const headline =
    fit.level === 'over'
      ? 'Too much for this room'
      : fit.level === 'crowded'
        ? 'This will feel crowded'
        : 'Fits comfortably'
  return (
    <div className="rounded-2xl border border-line bg-surface px-4 py-3.5 shadow-card">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className={`text-sm font-semibold ${meta.text}`}>{headline}</h3>
        <span className="text-xs text-muted">
          {fit.coveredM2.toFixed(1)} of {fit.floorM2.toFixed(1)} m² · {pct}%
        </span>
      </div>
      <div className="relative mt-2.5 h-2 overflow-hidden rounded-full bg-canvas">
        <div className={`h-full rounded-full transition-all duration-300 ${meta.bar}`} style={{ width: `${Math.min(100, pct)}%` }} />
        <div
          className="absolute inset-y-0 w-px bg-ink/40"
          style={{ left: `${ratio * 100}%` }}
          title={`Aim for under ${Math.round(ratio * 100)}%`}
        />
      </div>
      <ul className="mt-2.5 space-y-1 text-xs leading-relaxed text-muted">
        {fit.level === 'crowded' && (
          <li>Furniture covers {pct}% of the floor. Aim for under {Math.round(ratio * 100)}% so there&apos;s room to walk.</li>
        )}
        {fit.level === 'over' && <li>These pieces need more floor than the room has.</li>}
        {fit.tooBig.map((piece) => (
          <li key={piece.name} className="flex gap-1.5 text-rose">
            <AlertIcon size={14} className="mt-0.5 shrink-0" />
            <span>
              {piece.name} ({piece.lengthM} m) won&apos;t fit this room.
            </span>
          </li>
        ))}
        {fit.uncounted > 0 && (
          <li>
            {fit.uncounted} {fit.uncounted === 1 ? 'piece has' : 'pieces have'} no recorded size, so{' '}
            {fit.uncounted === 1 ? "it isn't" : "they aren't"} counted.
          </li>
        )}
        <li>Rugs, lighting and decor aren&apos;t counted.</li>
      </ul>
    </div>
  )
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-2 block text-sm font-semibold text-ink">
        {label}
      </label>
      {children}
    </div>
  )
}

function SideInput({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string
  value: string
  min: number
  max: number
  onChange: (value: string) => void
}) {
  return (
    <input
      type="number"
      inputMode="decimal"
      min={min}
      max={max}
      step={0.1}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      className={`${fieldClass} w-20`}
    />
  )
}
