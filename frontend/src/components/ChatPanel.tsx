import { useEffect, useRef } from 'react'
import type { FinderObject } from '../api/types'
import type { Turn } from '../hooks/useChat'
import { deriveQuickReplies } from '../lib/quickReplies'
import { Composer } from './Composer'
import { EmptyState } from './EmptyState'
import { ErrorCard } from './ErrorCard'
import { AssistantBubble, UserBubble, ZoryAvatar } from './MessageBubble'
import { PhotoTurn } from './PhotoTurn'
import { QuickReplies } from './QuickReplies'
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

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div className="flex-1 overflow-y-auto">
        {turns.length === 0 && !sending ? (
          <EmptyState storeId={storeId} onPick={onSend} onPhoto={onPhoto} />
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
            {turns.map((turn) => {
              if (turn.kind === 'user') return <UserBubble key={turn.id} text={turn.text} />
              if (turn.kind === 'assistant') return <AssistantBubble key={turn.id} data={turn.data} />
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
            {quickReplies.length > 0 && (
              <div className="pl-11">
                <QuickReplies replies={quickReplies} onPick={onSend} disabled={sending} />
              </div>
            )}
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
