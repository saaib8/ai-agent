import type { ChatResponse, RenderView } from '../api/types'
import type { RejectedRef } from '../hooks/useChat'
import type { QuickReply } from '../lib/quickReplies'
import { HelpIcon } from './icons'
import { ComparisonTable } from './presentation/ComparisonTable'
import { ProductGrid } from './presentation/ProductGrid'
import type { SearchRefineControls } from './presentation/ProductGrid'
import { RoomBundle } from './presentation/RoomBundle'
import { RoomRender } from './presentation/RoomRender'
import { QuickReplies } from './QuickReplies'
import { RawJson } from './RawJson'

export function UserBubble({ text, rejected }: { text: string; rejected?: RejectedRef }) {
  // A "Not this one" tap: show the product that was dismissed, so the thread
  // makes clear what was passed on rather than a bare line of text.
  if (rejected) {
    return (
      <div className="flex animate-rise justify-end">
        <div className="flex max-w-[80%] items-center gap-3 rounded-2xl rounded-br-md border border-line bg-surface px-3 py-2.5 shadow-card">
          <div className="h-11 w-11 shrink-0 overflow-hidden rounded-lg bg-canvas">
            {rejected.imageUrl && (
              <img
                src={rejected.imageUrl}
                alt=""
                className="h-full w-full object-cover opacity-55"
              />
            )}
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">
              Not this one
            </div>
            <div className="truncate text-sm text-ink line-through decoration-muted/50">
              {rejected.name}
            </div>
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className="flex animate-rise justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[15px] leading-relaxed text-white shadow-card">
        {text}
      </div>
    </div>
  )
}

export function ZoryAvatar() {
  return (
    <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-clay font-display text-sm font-semibold text-white">
      Z
    </div>
  )
}

interface AssistantBubbleProps {
  data: ChatResponse
  busy?: boolean
  /** Start swapping a room piece: opens the alternatives picker for that role. */
  onSwapStart?: (bundleOrdinal: number, role: string) => void
  /** Present only while choosing a replacement: turns product cards into pickers. */
  pick?: { role: string; onPick: (alternativeOrdinal: number) => void }
  /** Present only on the latest search grid: "different options" and per-card exclude. */
  refine?: SearchRefineControls
  /** Tappable answers for the follow-up question, on the latest turn only. */
  quickReplies?: QuickReply[]
  onQuickReply?: (value: string) => void
  /** Present on the current room package only: render it from a view. */
  onVisualize?: (view: RenderView, viewLabel: string) => void
  /** Present while this turn's render can be drawn again from another view. */
  onRerender?: (view: RenderView, viewLabel: string) => void
  /** A catalogue render: reopen the catalogue with its pieces. */
  onEditSelection?: () => void
  /** A render on this turn no longer matches the room package. */
  renderOutdated?: boolean
}

export function AssistantBubble({
  data,
  busy,
  onSwapStart,
  pick,
  refine,
  quickReplies,
  onQuickReply,
  onVisualize,
  onRerender,
  onEditSelection,
  renderOutdated = false,
}: AssistantBubbleProps) {
  const { response, presentation } = data
  const hasProducts = !!presentation?.products?.length
  const hasComparison = !!presentation?.comparison
  const hasRoom = !!presentation?.room
  const render = presentation?.render ?? null

  return (
    <div className="flex animate-rise gap-3">
      <ZoryAvatar />
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-2.5 text-[15px] leading-relaxed text-ink shadow-card">
          {response.message}
        </div>

        {response.follow_up_question && (
          <div className="flex max-w-[85%] items-start gap-2.5 rounded-xl border border-clay/15 bg-clay-soft/40 px-3.5 py-2.5">
            <HelpIcon size={16} className="mt-0.5 shrink-0 text-clay" />
            <div className="text-sm leading-relaxed text-ink">{response.follow_up_question}</div>
          </div>
        )}

        {quickReplies && quickReplies.length > 0 && onQuickReply && (
          <QuickReplies replies={quickReplies} onPick={onQuickReply} disabled={!!busy} />
        )}

        {hasProducts && (
          <ProductGrid products={presentation!.products} pick={pick} refine={refine} busy={busy} />
        )}
        {hasComparison && <ComparisonTable comparison={presentation!.comparison!} />}
        {hasRoom && (
          <RoomBundle
            room={presentation!.room!}
            onSwapStart={onSwapStart}
            busy={busy}
            onVisualize={onVisualize}
          />
        )}
        {render && (
          <RoomRender
            render={render}
            outdated={renderOutdated}
            busy={busy}
            onRerender={onRerender}
            onEdit={onEditSelection}
          />
        )}

        <RawJson value={data} />
      </div>
    </div>
  )
}
