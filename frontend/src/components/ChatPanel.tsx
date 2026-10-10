import { useEffect, useRef } from 'react'
import type {
  BriefAnswerAction,
  BriefChip,
  BundleAction,
  CatalogSelection,
  FinderObject,
  GroundedBundlePresentation,
  GroundedProduct,
  LikedView,
  PickView,
  ProductAction,
  RenderView,
  RoomRenderPresentation,
  SearchAction,
} from '../api/types'
import type { Activity, Turn } from '../hooks/useChat'
import { CompareBar } from './CompareBar'
import { Composer } from './Composer'
import { EmptyState } from './EmptyState'
import { ErrorCard } from './ErrorCard'
import { AssistantBubble, UserBubble, ZoryAvatar } from './MessageBubble'
import { PhotoTurn } from './PhotoTurn'
import { PicksTray } from './PicksTray'
import type { CheckedCard } from '../lib/compare'
import { TypingIndicator } from './TypingIndicator'

interface ChatPanelProps {
  turns: Turn[]
  sending: boolean
  /** A reply is typing out progressively (after validation); Stop freezes it. */
  revealing: boolean
  revealedLen: number
  revealTurnId: string | null
  activity: Activity
  storeId: number
  draft: string
  onDraftChange: (value: string) => void
  onSend: (text: string) => void
  /** Abort the in-flight reply; the send button is a Stop button while sending. */
  onStop: () => void
  onPhoto: (file: File) => void
  onPickObject: (photoTurnId: string, imageId: string, object: FinderObject) => void
  /** The role being swapped, when a swap is in progress; drives the picker. */
  swapRole: string | null
  onSwapStart: (bundleOrdinal: number, role: string) => void
  onPickAlternative: (alternativeOrdinal: number) => void
  onShowMoreOptions: () => void
  onExcludeProduct: (product: GroundedProduct) => void
  onVisualize: (view: RenderView, viewLabel: string) => void
  /** Place the room package in the customer's own room: a new photo, or
   *  (null) the one the session keeps. */
  onVisualizeInRoom: (file: File | null) => void
  /** Draw a package render again in the room photo it was drawn in. */
  onRerenderInRoom: (roomPhotoId: string) => void
  /** The room photo the session keeps, as a picture to show. */
  roomPhotoPreview: string | null
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
  /** Their liked list; null while unknown or when the buttons are off. */
  liked: LikedView[] | null
  /** ♡ taps not yet saved, by `list_revision:ordinal`. */
  pendingLikes: Map<string, 'like' | 'unlike'>
  onToggleLike: (product: GroundedProduct, listRevision: number) => void
  onMoreLikeThis: (product: GroundedProduct, listRevision: number) => void
  onUnlike: (liked: LikedView) => void
  onSelectLiked: (liked: LikedView) => void
  onMoreLikeThisLiked: (liked: LikedView) => void
  /** Cards checked for comparison, how to check one, and comparing them. */
  comparing: CheckedCard[]
  /** How many products one comparison may cover. */
  compareMax: number
  familyOf: (product: GroundedProduct) => string | null
  onToggleCompare: (product: GroundedProduct, listRevision: number) => void
  onCompare: () => void
  onUncheckCompare: (card: CheckedCard) => void
  onClearCompare: () => void
  /** A tapped chip: its words, and the action it runs when it carries one. */
  onChoice: (
    value: string,
    action?: ProductAction | null,
    bundle?: BundleAction | null,
    search?: SearchAction | null,
  ) => void
  /** Answers tapped on a card of questions. */
  onBriefSubmit: (answer: BriefAnswerAction | null, summary: string) => void
  onDropChip: (chip: BriefChip) => void
  onCombination: (op: 'choose' | 'dismiss' | 'more', position: number | null) => void
}

/** The latest result list and the six before it - as many as the server
 *  keeps tickable. */
const TICKABLE_LISTS = 7

export function ChatPanel({
  turns,
  sending,
  revealing,
  revealedLen,
  revealTurnId,
  activity,
  storeId,
  draft,
  onDraftChange,
  onSend,
  onStop,
  onPhoto,
  onPickObject,
  swapRole,
  onSwapStart,
  onPickAlternative,
  onShowMoreOptions,
  onExcludeProduct,
  onVisualize,
  onVisualizeInRoom,
  onRerenderInRoom,
  roomPhotoPreview,
  onOpenCatalog,
  onRerenderSelection,
  onEditSelection,
  picks,
  picking,
  picksError,
  onTogglePick,
  onRemovePick,
  onGoesWith,
  liked,
  pendingLikes,
  onToggleLike,
  onMoreLikeThis,
  onUnlike,
  onSelectLiked,
  onMoreLikeThisLiked,
  comparing,
  compareMax,
  familyOf,
  onToggleCompare,
  onCompare,
  onUncheckCompare,
  onClearCompare,
  onChoice,
  onBriefSubmit,
  onDropChip,
  onCombination,
}: ChatPanelProps) {
  const endRef = useRef<HTMLDivElement>(null)

  // Again when a reply finishes typing: its cards are drawn only then.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, sending, revealing])

  const last = turns[turns.length - 1]
  // The backend owns the question and its answers. Prose never creates controls.
  const backendChoices = last?.kind === 'assistant' ? last.data.presentation?.choices ?? [] : []
  const quickReplies =
    !sending && last?.kind === 'assistant' ? backendChoices : []

  // The picker only lives on the most recent assistant turn: older product
  // grids are history and must not sprout "Use this" buttons.
  const lastAssistantId = [...turns].reverse().find((t) => t.kind === 'assistant')?.id
  // What a list's search uses stays beside it until another list replaces it:
  // a turn that shows nothing new (a stale ✕, nothing more to page) leaves it.
  // Narrow down opened as a turn's question replaces the folded one, whose
  // answers would no longer be read.
  const briefTurnId = [...turns]
    .reverse()
    .filter((t) => t.kind === 'assistant')
    .find((t) => {
      const shown = t.data.presentation
      return (
        !!shown?.products?.length ||
        !!shown?.brief_chips?.length ||
        !!shown?.narrow_down ||
        shown?.brief?.mode === 'narrow'
      )
    })?.id

  // A tick and a reply both write the session, so neither starts while the
  // other is in flight.
  const busy = sending || picking || revealing

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
  // Compare checkboxes live on the same result lists as ticks. Only similar
  // products compare: once one is checked, other kinds are greyed out.
  const compareFor = (listRevision: number | null | undefined) =>
    listRevision != null && tickable.has(listRevision)
      ? {
          compareFor: (product: GroundedProduct) => {
            const ordinal = product.presented_ordinal
            const family = familyOf(product)
            if (ordinal == null || family == null) return undefined
            const isThis = (c: CheckedCard) =>
              c.listRevision === listRevision && c.ordinal === ordinal
            const checked = comparing.some(isThis)
            const other = comparing.find((c) => !isThis(c))
            const otherKind = other !== undefined && other.family !== family
            return {
              checked,
              disabled: !checked && (comparing.length >= compareMax || otherKind),
              hint: otherKind
                ? 'Compare it with products of the same kind'
                : `Up to ${compareMax} products at a time - uncheck one first`,
              onToggle: (p: GroundedProduct) => onToggleCompare(p, listRevision),
            }
          },
        }
      : undefined

  const selectionFor = (listRevision: number | null | undefined) =>
    listRevision != null && tickable.has(listRevision)
      ? {
          pickedOrdinals: pickedOn(listRevision),
          onToggle: (product: GroundedProduct) => onTogglePick(product, listRevision),
        }
      : undefined

  // ♡ and More like this live on the same result lists as ticks, when the
  // server reports a liked list at all.
  const likesFor = (listRevision: number | null | undefined) =>
    liked !== null && listRevision != null && tickable.has(listRevision)
      ? {
          // What the server holds, as the queued taps will leave it.
          likedOrdinals: new Set(
            [
              ...liked.flatMap((l) =>
                (l.positions ?? [])
                  .filter((position) => position.list_revision === listRevision)
                  .map((position) => position.ordinal),
              ),
              ...[...pendingLikes].flatMap(([key, kind]) => {
                const [revision, ordinal] = key.split(':').map(Number)
                return revision === listRevision && kind === 'like' ? [ordinal] : []
              }),
            ].filter((ordinal) => pendingLikes.get(`${listRevision}:${ordinal}`) !== 'unlike'),
          ),
          onToggle: (product: GroundedProduct) => onToggleLike(product, listRevision),
        }
      : undefined
  const moreLikeThisFor = (listRevision: number | null | undefined) =>
    liked !== null && listRevision != null && tickable.has(listRevision)
      ? (product: GroundedProduct) => onMoreLikeThis(product, listRevision)
      : undefined

  // Their liked list drawn as cards: each card is found in the liked list
  // as the server reports it now, and acts by its place there.
  const sameProduct = (a: GroundedProduct, b: { name_english: string; image_url: string }) =>
    a.name_english === b.name_english && a.image_url === b.image_url
  const likedOf = (product: GroundedProduct) => (liked ?? []).find((l) => sameProduct(product, l))
  const pickOf = (product: GroundedProduct) => picks.find((p) => sameProduct(product, p))
  const ordinalsWhere = (products: GroundedProduct[], test: (p: GroundedProduct) => boolean) =>
    new Set(
      products.flatMap((p) => (p.presented_ordinal != null && test(p) ? [p.presented_ordinal] : [])),
    )
  const likedCards = (products: GroundedProduct[]) =>
    liked === null
      ? {}
      : {
          selection: {
            pickedOrdinals: ordinalsWhere(products, (p) => !!pickOf(p)),
            onToggle: (product: GroundedProduct) => {
              const pick = pickOf(product)
              const like = likedOf(product)
              if (pick) onRemovePick(pick)
              else if (like) onSelectLiked(like)
            },
          },
          likes: {
            likedOrdinals: ordinalsWhere(products, (p) => !!likedOf(p)),
            onToggle: (product: GroundedProduct) => {
              const like = likedOf(product)
              if (like) onUnlike(like)
            },
          },
          onMoreLikeThis: (product: GroundedProduct) => {
            const like = likedOf(product)
            if (like) onMoreLikeThisLiked(like)
          },
        }

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
    if (room)
      return turnId === currentRoomId ? { onVisualize, onVisualizeInRoom, roomPhotoPreview } : {}
    // A catalogue render pictures the customer's own picks, which no later
    // turn changes: it never goes out of date.
    if (render && selection)
      return {
        onRerender: (view: RenderView, label: string) => onRerenderSelection(selection, view, label),
        onEditSelection: () => onEditSelection(selection, render.view ?? 'corner'),
      }
    if (render?.source === 'catalog') return {}
    if (render) {
      const outdated = currentRoomKey !== null && renderKey(render) !== currentRoomKey
      const photoId = render.room_photo_id
      const again = photoId ? () => onRerenderInRoom(photoId) : onVisualize
      return { renderOutdated: outdated, ...(outdated ? {} : { onRerender: again }) }
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
                return (
                  <UserBubble
                    key={turn.id}
                    text={turn.text}
                    rejected={turn.rejected}
                    imageUrl={turn.imageUrl}
                  />
                )
              if (turn.kind === 'assistant')
                return (
                  <AssistantBubble
                    key={turn.id}
                    data={turn.data}
                    busy={busy}
                    revealedLen={turn.id === revealTurnId ? revealedLen : undefined}
                    revealing={turn.id === revealTurnId && revealing}
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
                    compare={compareFor(turn.data.presentation?.list_revision)}
                    likes={likesFor(turn.data.presentation?.list_revision)}
                    onMoreLikeThis={moreLikeThisFor(turn.data.presentation?.list_revision)}
                    {...(turn.data.presentation?.product_source === 'liked'
                      ? likedCards(turn.data.presentation.products)
                      : {})}
                    onBriefSubmit={onBriefSubmit}
                    onDropChip={onDropChip}
                    onCombination={turn.id === lastAssistantId ? onCombination : undefined}
                    showBrief={turn.id === briefTurnId}
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
                  {activity === 'preparing_room' && (
                    <span className="text-sm text-muted">
                      Clearing your room — this takes about 30 seconds…
                    </span>
                  )}
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <CompareBar
        checked={comparing}
        busy={busy}
        onCompare={onCompare}
        onUncheck={onUncheckCompare}
        onClear={onClearCompare}
      />
      <PicksTray
        picks={picks}
        busy={busy}
        error={picksError}
        onRemove={onRemovePick}
        onGoesWith={onGoesWith}
      />
      <Composer
        value={draft}
        onChange={onDraftChange}
        onSend={() => onSend(draft)}
        onStop={onStop}
        onPhoto={onPhoto}
        onOpenCatalog={onOpenCatalog}
        disabled={busy}
        sending={sending || revealing}
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
