import { useEffect, useRef } from 'react'
import type { FinderObject, GroundedProduct } from '../api/types'
import type { Turn } from '../hooks/useChat'
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
}

export function ChatPanel({
  turns,
  sending,
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
                <div className="rounded-2xl rounded-tl-md border border-line bg-surface px-3.5 py-2.5 shadow-card">
                  <TypingIndicator />
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
