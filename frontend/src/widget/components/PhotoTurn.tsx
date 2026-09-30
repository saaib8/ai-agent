import { useState } from 'react'
import type { FinderObject } from '../../api/types'
import { useWidget } from '../context'
import type { PhotoState } from '../types'

/**
 * A photo the shopper shared, with the detector's outlines drawn over it.
 * Outlines are in the stored photo's pixel space, so the overlay's viewBox is
 * that size, stretched over the image (same aspect ratio: the backend only
 * rotates by EXIF and scales uniformly). Larger objects are drawn first so a
 * small side table on a large rug stays tappable.
 */
export function PhotoTurn({ id, url, photo }: { id: string; url: string; photo: PhotoState }) {
  const { agent } = useWidget()
  const [hovered, setHovered] = useState<number | null>(null)
  const ready = photo.status === 'ready' ? photo : null
  const objects = ready?.data.objects ?? []
  const ordered = [...objects].sort((a, b) => area(b) - area(a))
  const busy = agent.sending
  const pick = (object: FinderObject) => {
    if (ready && !busy) void agent.pickObject(id, ready.data.image_id, object)
  }

  return (
    <>
      <div className="photo">
        <img src={url} alt="Your photo" />
        {ready && (
          <svg
            viewBox={`0 0 ${ready.data.width} ${ready.data.height}`}
            preserveAspectRatio="none"
            role="group"
            aria-label="Pieces found in your photo"
          >
            {ordered.map((object) => {
              const active = hovered === object.object_id
              const picked = ready.picked.includes(object.object_id)
              return (
                <polygon
                  key={object.object_id}
                  points={object.polygon.map(([x, y]) => `${x},${y}`).join(' ')}
                  vectorEffect="non-scaling-stroke"
                  fill={active ? 'rgba(62,141,168,0.32)' : picked ? 'rgba(62,141,168,0.16)' : 'rgba(255,255,255,0.08)'}
                  stroke={active || picked ? 'var(--accent)' : 'rgba(255,255,255,0.9)'}
                  strokeWidth={active ? 3 : 2}
                  strokeDasharray={active || picked ? undefined : '6 4'}
                  onMouseEnter={() => setHovered(object.object_id)}
                  onMouseLeave={() => setHovered(null)}
                  onClick={() => pick(object)}
                >
                  <title>{object.display_label}</title>
                </polygon>
              )
            })}
          </svg>
        )}
        {photo.status === 'detecting' && (
          <div className="photo-status">
            <span>
              <span className="dots">
                <i />
                <i />
                <i />
              </span>
              Finding pieces in your photo
            </span>
          </div>
        )}
      </div>

      {ready && (
        <div className="photo-meta">
          {objects.length > 0 ? (
            <>
              <span>Tap a piece to find similar ones in the store.</span>
              <div className="chips">
                {objects.map((object) => (
                  <button
                    key={object.object_id}
                    className="chip cap"
                    aria-pressed={ready.picked.includes(object.object_id)}
                    onMouseEnter={() => setHovered(object.object_id)}
                    onMouseLeave={() => setHovered(null)}
                    onClick={() => pick(object)}
                    disabled={busy}
                  >
                    {object.display_label}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <span>I couldn&apos;t spot anything in this photo that the store carries. Try a photo with the furniture clearly in view.</span>
          )}
        </div>
      )}
      {photo.status === 'error' && <div className="bubble-error" style={{ alignSelf: 'flex-end' }}>{photo.message}</div>}
    </>
  )
}

function area(o: FinderObject): number {
  return (o.box.x2 - o.box.x1) * (o.box.y2 - o.box.y1)
}
