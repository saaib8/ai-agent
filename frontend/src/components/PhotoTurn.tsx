import { useState } from 'react'
import type { FinderObject } from '../api/types'
import type { PhotoState } from '../hooks/useChat'
import { ErrorCard } from './ErrorCard'
import { TypingIndicator } from './TypingIndicator'

interface PhotoTurnProps {
  url: string
  photo: PhotoState
  disabled: boolean
  onPick: (imageId: string, object: FinderObject) => void
}

/**
 * A photo the customer shared, drawn with the detector's outlines on top.
 *
 * The outlines are in the pixel space of the photo the backend stored, so the
 * overlay's viewBox is that size and it is stretched over the image. The
 * browser shows the original upload, which has the same aspect ratio (the
 * backend only rotates by EXIF, as the browser does, and scales uniformly).
 *
 * Larger objects are drawn first, so a small side table on top of a large rug
 * stays clickable. Where the detector gave one outline two labels (a "sofa"
 * that may be a "2-seater sofa"), the more confident label is drawn on top and
 * is the one a click on the photo picks; the chips below offer both.
 */
export function PhotoTurn({ url, photo, disabled, onPick }: PhotoTurnProps) {
  const [hovered, setHovered] = useState<number | null>(null)
  const ready = photo.status === 'ready' ? photo : null
  const objects = ready?.data.objects ?? []
  const drawOrder = [...objects].sort((a, b) => area(b) - area(a) || a.confidence - b.confidence)

  return (
    <div className="flex animate-rise flex-col items-end gap-2">
      <div className="relative w-full max-w-[520px] overflow-hidden rounded-2xl rounded-br-md border border-line bg-surface-muted shadow-card">
        <img src={url} alt="Your photo" className="block h-auto w-full" />

        {ready && (
          <svg
            viewBox={`0 0 ${ready.data.width} ${ready.data.height}`}
            preserveAspectRatio="none"
            className="absolute inset-0 h-full w-full"
            role="group"
            aria-label="Items found in your photo"
          >
            {drawOrder.map((o) => {
              const active = hovered === o.object_id
              const picked = ready.picked.includes(o.object_id)
              return (
                <polygon
                  key={o.object_id}
                  points={o.polygon.map(([x, y]) => `${x},${y}`).join(' ')}
                  vectorEffect="non-scaling-stroke"
                  className={`transition ${disabled ? 'cursor-wait' : 'cursor-pointer'}`}
                  fill={active ? 'rgb(173 86 55 / 0.28)' : picked ? 'rgb(173 86 55 / 0.14)' : 'rgb(255 255 255 / 0.06)'}
                  stroke={active || picked ? '#ad5637' : 'rgb(255 255 255 / 0.85)'}
                  strokeWidth={active ? 3 : 2}
                  strokeDasharray={active || picked ? undefined : '6 4'}
                  onMouseEnter={() => setHovered(o.object_id)}
                  onMouseLeave={() => setHovered(null)}
                  onClick={() => !disabled && onPick(ready.data.image_id, o)}
                >
                  <title>{o.display_label}</title>
                </polygon>
              )
            })}
          </svg>
        )}

        {photo.status === 'detecting' && (
          <div className="absolute inset-0 flex items-center justify-center bg-ink/35 backdrop-blur-[1px]">
            <div className="flex items-center gap-2.5 rounded-full bg-surface/95 px-3.5 py-2 text-sm text-ink shadow-soft">
              <TypingIndicator />
              <span>Detecting objects in image</span>
            </div>
          </div>
        )}
      </div>

      {ready && (
        <div className="w-full max-w-[520px]">
          {objects.length > 0 ? (
            <>
              <p className="mb-1.5 text-right text-xs text-muted">
                Tap an item in the photo, or pick one here, to find similar products.
              </p>
              <div className="flex flex-wrap justify-end gap-1.5">
                {objects.map((o) => {
                  const picked = ready.picked.includes(o.object_id)
                  return (
                    <button
                      key={o.object_id}
                      disabled={disabled}
                      onMouseEnter={() => setHovered(o.object_id)}
                      onMouseLeave={() => setHovered(null)}
                      onFocus={() => setHovered(o.object_id)}
                      onBlur={() => setHovered(null)}
                      onClick={() => onPick(ready.data.image_id, o)}
                      className={`rounded-full border px-3 py-1 text-xs capitalize transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:opacity-60 ${
                        picked
                          ? 'border-clay/40 bg-clay-soft/60 text-clay'
                          : 'border-line bg-surface text-ink hover:border-clay/40 hover:text-clay'
                      }`}
                    >
                      {o.display_label}
                    </button>
                  )
                })}
              </div>
            </>
          ) : (
            <p className="text-right text-xs text-muted">
              I couldn&apos;t spot anything in this photo that this catalog carries. Try a photo
              with the furniture more clearly in view.
            </p>
          )}
          {ready.data.unmatched_count > 0 && objects.length > 0 && (
            <p className="mt-1 text-right text-[11px] text-muted/80">
              {ready.data.unmatched_count} other item{ready.data.unmatched_count === 1 ? '' : 's'}{' '}
              in the photo can&apos;t be matched in this catalog.
            </p>
          )}
        </div>
      )}

      {photo.status === 'error' && (
        <div className="w-full max-w-[520px]">
          <ErrorCard status={photo.httpStatus} error={photo.error} />
        </div>
      )}
    </div>
  )
}

function area(o: FinderObject): number {
  return (o.box.x2 - o.box.x1) * (o.box.y2 - o.box.y1)
}
