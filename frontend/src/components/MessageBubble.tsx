import type {
  BriefAnswerAction,
  BundleAction,
  ChatResponse,
  ProductAction,
  RenderView,
} from '../api/types'
import type { RejectedRef } from '../hooks/useChat'
import type { ReplyChoice } from '../api/types'
import { BriefCard } from './BriefCard'
import { HelpIcon } from './icons'
import { ComparisonTable } from './presentation/ComparisonTable'
import { FocusCard } from './presentation/FocusCard'
import { ProductGrid } from './presentation/ProductGrid'
import type {
  GridCompare,
  GridSelection,
  SearchRefineControls,
} from './presentation/ProductGrid'
import { RoomBundle } from './presentation/RoomBundle'
import { RoomRender } from './presentation/RoomRender'
import { PiecePicker } from './PiecePicker'
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
              {rejected.label ?? 'Not this one'}
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
  /** Present on the search results on screen: tick cards into the picks. */
  selection?: GridSelection
  /** Present on the search results on screen: check cards to compare. */
  compare?: GridCompare
  /** Tappable answers for the follow-up question, on the latest turn only. */
  quickReplies?: ReplyChoice[]
  onQuickReply?: (value: string, action?: ProductAction | null, bundle?: BundleAction | null) => void
  /** Present on the current room package only: render it from a view. */
  onVisualize?: (view: RenderView, viewLabel: string) => void
  /** Present while this turn's render can be drawn again from another view. */
  onRerender?: (view: RenderView, viewLabel: string) => void
  /** A catalogue render: reopen the catalogue with its pieces. */
  onEditSelection?: () => void
  /** A render on this turn no longer matches the room package. */
  renderOutdated?: boolean
  /** The most recent assistant turn: only it offers interactive pickers. */
  latest?: boolean
  /** Answers tapped on a card of questions, sent as a search. */
  onBriefSubmit?: (answer: BriefAnswerAction, summary: string) => void
}

export function AssistantBubble({
  data,
  busy,
  onSwapStart,
  pick,
  refine,
  selection,
  compare,
  quickReplies,
  onQuickReply,
  onVisualize,
  onRerender,
  onEditSelection,
  renderOutdated = false,
  latest,
  onBriefSubmit,
}: AssistantBubbleProps) {
  const { response, presentation } = data
  const hasProducts = !!presentation?.products?.length
  // Only search results are the list a tick or "Not this one" counts into.
  // Picks shown back, or a single product, would act on the list behind them.
  const isResultList = presentation?.product_source === 'search'
  const focus = presentation?.focus ?? null
  // Chips that run an action - the companions of a product - belong under the
  // cards they extend; plain answers to a question stay beside the question.
  // Offered beside a pick with nothing searched, the kinds and "No thanks"
  // are one row under the pick.
  const offering =
    !!focus && !hasProducts && (quickReplies ?? []).some((r) => r.product_action)
  const textReplies = offering ? [] : (quickReplies ?? []).filter((r) => !r.product_action)
  const actionReplies = offering
    ? quickReplies ?? []
    : (quickReplies ?? []).filter((r) => r.product_action)
  const hasComparison = !!presentation?.comparison
  const hasRoom = !!presentation?.room
  const render = presentation?.render ?? null
  const seatingBundles = presentation?.seating_bundles ?? []
  const brief = presentation?.brief ?? null
  const briefCard = brief && onBriefSubmit && (
    <BriefCard brief={brief} active={!!latest} busy={!!busy} onSubmit={onBriefSubmit} />
  )

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

        {textReplies.length > 0 && onQuickReply && (
          <QuickReplies replies={textReplies} onPick={onQuickReply} disabled={!!busy} />
        )}

        {latest && presentation?.piece_picker && onQuickReply && (
          <PiecePicker picker={presentation.piece_picker} onSend={onQuickReply} disabled={!!busy} />
        )}

        {brief?.mode === 'ask' && briefCard}

        {focus && <FocusCard product={focus} />}
        {hasProducts && (
          <ProductGrid
            products={presentation!.products}
            pick={pick}
            refine={isResultList ? refine : undefined}
            selection={isResultList ? selection : undefined}
            compare={isResultList ? compare : undefined}
            bestMatch={isResultList && !!presentation?.best_match}
            busy={busy}
          />
        )}
        {brief?.mode === 'narrow' && latest && briefCard}
        {actionReplies.length > 0 && onQuickReply && (
          <QuickReplies
            replies={actionReplies}
            onPick={onQuickReply}
            disabled={!!busy}
            label={offering ? 'Goes well with it' : 'Also goes well with it'}
          />
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

        {seatingBundles.length > 0 && (
          <div className="flex flex-col gap-2.5">
            {seatingBundles.map((bundle, index) => (
              <RoomBundle
                key={index}
                room={bundle}
                label={
                  seatingBundles.length > 1 ? `Seating option ${index + 1}` : 'Seating combination'
                }
                hideStatus
              />
            ))}
          </div>
        )}

        <RawJson value={data} />
      </div>
    </div>
  )
}
