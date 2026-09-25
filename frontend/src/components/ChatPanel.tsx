import { useEffect, useRef } from 'react'
import type {
  FinderObject,
  GroundedBundlePresentation,
  GroundedProduct,
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
}

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
}: ChatPanelProps) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, sending])

  const last = turns[turns.length - 1]
  const quickReplies =
    !sending && last?.kind === 'assistant'
      ? deriveQuickReplies(last.data.response.message, last.data.response.follow_up_question)
      : []

  // The picker only lives on the most recent assistant turn: older product
  // grids are history and must not sprout "Use this" buttons.
  const lastAssistantId = [...turns].reverse().find((t) => t.kind === 'assistant')?.id

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
  ) => {
    if (room) return turnId === currentRoomId ? { onVisualize } : {}
    if (render) {
      const outdated = currentRoomKey !== null && renderKey(render) !== currentRoomKey
      return { renderOutdated: outdated, ...(outdated ? {} : { onVisualize }) }
    }
    return {}
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div className="flex-1 overflow-y-auto">
        {turns.length === 0 && !sending ? (
          <EmptyState storeId={storeId} onPick={onSend} onPhoto={onPhoto} />
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
                    busy={sending}
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
                    quickReplies={turn.id === lastAssistantId ? quickReplies : undefined}
                    onQuickReply={onSend}
                    {...visualizeProps(turn.data.presentation?.room ?? null, turn.data.presentation?.render ?? null, turn.id)}
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

      <Composer
        value={draft}
        onChange={onDraftChange}
        onSend={() => onSend(draft)}
        onPhoto={onPhoto}
        disabled={sending}
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
