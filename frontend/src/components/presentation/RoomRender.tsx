import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { RENDER_VIEWS } from '../../api/types'
import type { RenderView, RoomRenderPresentation } from '../../api/types'
import { CloseIcon, DownloadIcon, ExpandIcon, GridIcon, ImageIcon } from '../icons'
import { ViewPicker } from './ViewPicker'

interface RoomRenderProps {
  render: RoomRenderPresentation
  /** The room package changed after this render was made. */
  outdated: boolean
  busy?: boolean
  /** Present only while this render still shows what it pictured. */
  onRerender?: (view: RenderView, viewLabel: string) => void
  /** A catalogue render: reopen the catalogue with its pieces and room. */
  onEdit?: () => void
}

/**
 * A picture of a room: full-screen on tap, downloadable, and re-renderable
 * from another view while it still shows what it pictured. A catalogue render
 * can also be reopened in the catalogue to change its pieces.
 */
export function RoomRender({ render, outdated, busy = false, onRerender, onEdit }: RoomRenderProps) {
  const [fullScreen, setFullScreen] = useState(false)
  const firstOther = RENDER_VIEWS.find((v) => v.value !== render.view)?.value ?? render.view
  const [nextView, setNextView] = useState<RenderView>(firstOther)
  const nextLabel = RENDER_VIEWS.find((v) => v.value === nextView)?.label ?? ''

  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      <div className="flex items-center justify-between border-b border-line bg-canvas/60 px-4 py-3">
        <span className="text-sm font-semibold text-ink">Your room</span>
        <span className="rounded-full bg-canvas px-2.5 py-0.5 text-[11px] font-medium text-muted">
          {render.view_label} view
        </span>
      </div>

      {outdated && (
        <div className="border-b border-amber/20 bg-amber/10 px-4 py-2 text-xs text-amber">
          Package changed since this render.
        </div>
      )}

      <button
        onClick={() => setFullScreen(true)}
        aria-label="Open full screen"
        className="group relative block w-full bg-canvas focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-clay/40"
        style={{ aspectRatio: `${render.width} / ${render.height}` }}
      >
        <img
          src={render.image_url}
          alt={`Your room, ${render.view_label.toLowerCase()} view`}
          className={`h-full w-full object-cover ${outdated ? 'opacity-70' : ''}`}
        />
        <span className="absolute right-3 top-3 rounded-full bg-ink/55 p-1.5 text-white opacity-0 transition group-hover:opacity-100">
          <ExpandIcon size={16} />
        </span>
      </button>

      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <ActionButton onClick={() => setFullScreen(true)}>
          <ExpandIcon size={14} />
          Full screen
        </ActionButton>
        <ActionButton onClick={() => void download(render)}>
          <DownloadIcon size={14} />
          Download
        </ActionButton>
        {onEdit && (
          <ActionButton onClick={onEdit} disabled={busy}>
            <GridIcon size={14} />
            Edit selection
          </ActionButton>
        )}

        {onRerender && !outdated && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <ViewPicker value={nextView} onChange={setNextView} disabled={busy} />
            <button
              onClick={() => onRerender(nextView, nextLabel)}
              disabled={busy || nextView === render.view}
              className="inline-flex items-center gap-1.5 rounded-full border border-clay/30 px-3 py-1.5 text-xs font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ImageIcon size={14} />
              Try this view
            </button>
          </div>
        )}
      </div>

      {fullScreen && <Lightbox render={render} onClose={() => setFullScreen(false)} />}
    </div>
  )
}

function ActionButton({
  onClick,
  disabled = false,
  children,
}: {
  onClick: () => void
  disabled?: boolean
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink transition hover:border-line-strong hover:bg-surface-muted/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  )
}

function Lightbox({ render, onClose }: { render: RoomRenderPresentation; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Room visualisation"
      onClick={onClose}
      className="fixed inset-0 z-50 flex animate-fade items-center justify-center bg-ink/85 p-4 sm:p-8"
    >
      <button
        onClick={onClose}
        aria-label="Close"
        className="absolute right-4 top-4 rounded-full bg-surface/15 p-2 text-white transition hover:bg-surface/25 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
      >
        <CloseIcon size={20} />
      </button>
      <img
        src={render.image_url}
        alt={`Your room, ${render.view_label.toLowerCase()} view`}
        onClick={(e) => e.stopPropagation()}
        className="max-h-full max-w-full rounded-xl object-contain shadow-soft"
      />
    </div>
  )
}

/**
 * Save the render. The picture arrived inside the reply as a data URL, so
 * nothing is fetched from a server: it is turned into a blob and saved. A blob
 * rather than the data URL itself, because browsers refuse to download very
 * long data URLs from a link.
 */
async function download(render: RoomRenderPresentation): Promise<void> {
  const blob = await (await fetch(render.image_url)).blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `zory-room-${render.view}.jpg`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
