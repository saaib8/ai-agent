import { useEffect, useRef } from 'react'
import type {
  BriefAnswerAction,
  CatalogSelection,
  FinderObject,
  GroundedBundlePresentation,
  GroundedProduct,
  PickView,
  ProductAction,
  RenderView,
  RoomRenderPresentation,
} from '../api/types'
import type { Activity, Turn } from '../hooks/useChat'
import { deriveQuickReplies } from '../lib/quickReplies'
import { Composer } from './Composer'
import { EmptyState } from './EmptyState'
import { ErrorCard } from './ErrorCard'
import { AssistantBubble, UserBubble, ZoryAvatar } from './MessageBubble'
import { PhotoTurn } from './PhotoTurn'
import { PicksTray } from './PicksTray'
import { TypingIndicator } from './TypingIndicator'

interface ChatPanelProps {
  turns: Turn[]
  sending: boolean
  activity: Activity
  storeId: number
  draft: string
  onDraftChange: (value: string) => void
  onSend: (text: string) => void
  onPhoto: (file: File) => void
  onPickObject: (photoTurnId: string, imageId: string, object: FinderObject) => void
  /** The role being swapped, when a swap is in progress; drives the picker. */
  swapRole: string | null
  onSwapStart: (bundleOrdinal: number, role: string) => void
  onPickAlternative: (alternativeOrdinal: number) => void
  onShowMoreOptions: () => void
  onExcludeProduct: (product: GroundedProduct) => void
  onVisualize: (view: RenderView, viewLabel: string) => void
  onOpenCatalog: () => void
  /** Draw a catalogue selection again, from another view. */
  onRerenderSelection: (selection: CatalogSelection, view: RenderView, viewLabel: string) => void
  /** Reopen the catalogue with a selection's pieces and room. */
  onEditSelection: (selection: CatalogSelection, view: RenderView) => void
  /** The customer's picks, as the server last reported them. */
  picks: PickView[]
  /** A tick is being saved; the session is busy. */
  picking: boolean
  picksError: string | null
  /** Tick or untick a card on the result list it belongs to. */
  onTogglePick: (product: GroundedProduct, listRevision: number) => void
  onRemovePick: (pick: PickView) => void
  /** Show what goes with a pick. */
  onGoesWith: (pick: PickView) => void
  onComparePicks: (first: PickView, second: PickView) => void
  /** A tapped chip: its words, and the action it runs when it carries one. */
  onChoice: (value: string, action?: ProductAction | null) => void
  /** Answers tapped on a card of questions. */
  onBriefSubmit: (answer: BriefAnswerAction, summary: string) => void
}

/** The latest result list and the six before it - as many as the server
 *  keeps tickable. */
const TICKABLE_LISTS = 7

export function ChatPanel({
  turns,
  sending,
  activity,
  storeId,
  draft,
  onDraftChange,
  onSend,
  onPhoto,
  onPickObject,
  swapRole,
  onSwapStart,
  onPickAlternative,
  onShowMoreOptions,
  onExcludeProduct,
  onVisualize,
  onOpenCatalog,
  onRerenderSelection,
  onEditSelection,
  picks,
  picking,
  picksError,
  onTogglePick,
  onRemovePick,
  onGoesWith,
  onComparePicks,
  onChoice,
  onBriefSubmit,
}: ChatPanelProps) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, sending])

  const last = turns[turns.length - 1]
  // The backend's own choices win: they are built from real options, where the
  // derived chips are only a guess from the question's wording.
  const backendChoices = last?.kind === 'assistant' ? last.data.presentation?.choices ?? [] : []
  // A card of questions or a piece picker is the turn's question: nothing is
  // guessed from the reply's words beside it.
  const quickReplies =
    !sending && last?.kind === 'assistant'
      ? backendChoices.length > 0 ||
        last.data.presentation?.piece_picker ||
        last.data.presentation?.brief
        ? backendChoices
        : deriveQuickReplies(last.data.response.message, last.data.response.follow_up_question)
      : []

  // The picker only lives on the most recent assistant turn: older product
  // grids are history and must not sprout "Use this" buttons.
  const lastAssistantId = [...turns].reverse().find((t) => t.kind === 'assistant')?.id

  // A tick and a reply both write the session, so neither starts while the
  // other is in flight.
  const busy = sending || picking

  // A tick names the result list its card is on. The server remembers the
  // latest list and a few before it, so a sofa can still be picked after the
  // screen has moved on to what goes with another; older grids are history.
  const tickable = new Set(
    turns
      .flatMap((t) =>
        t.kind === 'assistant' &&
        t.data.presentation?.product_source === 'search' &&
        t.data.presentation.list_revision != null
          ? [t.data.presentation.list_revision]
          : [],
      )
      .slice(-TICKABLE_LISTS),
  )
  const pickedOn = (listRevision: number) =>
    new Set(
      picks.flatMap((p) =>
        (p.positions ?? [])
          .filter((position) => position.list_revision === listRevision)
          .map((position) => position.ordinal),
      ),
    )
  const selectionFor = (listRevision: number | null | undefined) =>
    listRevision != null && tickable.has(listRevision)
      ? {
          pickedOrdinals: pickedOn(listRevision),
          onToggle: (product: GroundedProduct) => onTogglePick(product, listRevision),
        }
      : undefined

  // The room package on screen now: the latest turn that showed one. Only it
  // offers Visualize, and a render is outdated once its pieces differ from it.
  const currentRoom = [...turns]
    .reverse()
    .find((t) => t.kind === 'assistant' && !!t.data.presentation?.room)
  const currentRoomId = currentRoom?.id
  const currentRoomKey =
    currentRoom?.kind === 'assistant' && currentRoom.data.presentation?.room
      ? roomKey(currentRoom.data.presentation.room)
      : null

  const visualizeProps = (
    room: GroundedBundlePresentation | null,
    render: RoomRenderPresentation | null,
    turnId: string,
    selection: CatalogSelection | undefined,
  ) => {
    if (room) return turnId === currentRoomId ? { onVisualize } : {}
    // A catalogue render pictures the customer's own picks, which no later
    // turn changes: it never goes out of date.
    if (render && selection)
      return {
        onRerender: (view: RenderView, label: string) => onRerenderSelection(selection, view, label),
        onEditSelection: () => onEditSelection(selection, render.view),
      }
    if (render?.source === 'catalog') return {}
    if (render) {
      const outdated = currentRoomKey !== null && renderKey(render) !== currentRoomKey
      return { renderOutdated: outdated, ...(outdated ? {} : { onRerender: onVisualize }) }
    }
    return {}
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div className="flex-1 overflow-y-auto">
        {turns.length === 0 && !sending ? (
          <EmptyState
            storeId={storeId}
            onPick={onSend}
            onPhoto={onPhoto}
            onOpenCatalog={onOpenCatalog}
          />
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
            {turns.map((turn) => {
              if (turn.kind === 'user')
                return <UserBubble key={turn.id} text={turn.text} rejected={turn.rejected} />
              if (turn.kind === 'assistant')
                return (
                  <AssistantBubble
                    key={turn.id}
                    data={turn.data}
                    busy={busy}
                    onSwapStart={onSwapStart}
                    pick={
                      turn.id === lastAssistantId && swapRole
                        ? { role: swapRole, onPick: onPickAlternative }
                        : undefined
                    }
                    refine={
                      turn.id === lastAssistantId
                        ? { onShowMore: onShowMoreOptions, onExclude: onExcludeProduct }
                        : undefined
                    }
                    selection={selectionFor(turn.data.presentation?.list_revision)}
                    onBriefSubmit={onBriefSubmit}
                    quickReplies={turn.id === lastAssistantId ? quickReplies : undefined}
                    onQuickReply={onChoice}
                    latest={turn.id === lastAssistantId}
                    {...visualizeProps(
                      turn.data.presentation?.room ?? null,
                      turn.data.presentation?.render ?? null,
                      turn.id,
                      turn.selection,
                    )}
                  />
                )
              if (turn.kind === 'photo')
                return (
                  <PhotoTurn
                    key={turn.id}
                    url={turn.url}
                    photo={turn.photo}
                    disabled={sending}
                    onPick={(imageId, object) => onPickObject(turn.id, imageId, object)}
                  />
                )
              return <ErrorCard key={turn.id} status={turn.status} error={turn.error} />
            })}
            {/* A photo being analysed shows its own progress over the image. */}
            {sending && !(last?.kind === 'photo' && last.photo.status === 'detecting') && (
              <div className="flex animate-rise gap-3">
                <ZoryAvatar />
                <div className="flex items-center gap-2.5 rounded-2xl rounded-tl-md border border-line bg-surface px-3.5 py-2.5 shadow-card">
                  <TypingIndicator />
                  {activity === 'rendering' && (
                    <span className="text-sm text-muted">
                      Rendering your room — this takes about 30 seconds…
                    </span>
                  )}
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <PicksTray
        picks={picks}
        busy={busy}
        error={picksError}
        onRemove={onRemovePick}
        onGoesWith={onGoesWith}
        onCompare={onComparePicks}
      />
      <Composer
        value={draft}
        onChange={onDraftChange}
        onSend={() => onSend(draft)}
        onPhoto={onPhoto}
        onOpenCatalog={onOpenCatalog}
        disabled={busy}
      />
    </div>
  )
}

/** Which products, and how many of each, a package holds. Order-free. */
function roomKey(room: GroundedBundlePresentation): string {
  const units = new Map<string, number>()
  for (const item of room.items) {
    units.set(item.product_url, (units.get(item.product_url) ?? 0) + item.quantity)
  }
  return signature(units)
}

function renderKey(render: RoomRenderPresentation): string {
  return signature(new Map(render.items.map((item) => [item.product_url, item.quantity])))
}

function signature(units: Map<string, number>): string {
  return [...units].map(([url, n]) => `${url}×${n}`).sort().join('|')
}
