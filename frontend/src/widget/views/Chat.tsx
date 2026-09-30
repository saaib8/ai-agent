import { useEffect, useRef } from 'react'
import { Avatar } from '../components/Avatar'
import { AssistantTurn } from '../components/AssistantTurn'
import { PhotoTurn } from '../components/PhotoTurn'
import { useWidget } from '../context'
import type { Activity, Turn } from '../types'

const WAITING: Record<Activity, string> = {
  thinking: 'Thinking',
  rendering: 'Drawing your room — this can take a minute',
  detecting: 'Looking at your photo',
}

export function Chat() {
  const { config, agent } = useWidget()
  const end = useRef<HTMLDivElement>(null)
  const mounted = useRef(false)
  const turns = agent.turns
  const last = turns[turns.length - 1]

  // Follow the conversation: a new turn, a turn that changed (a photo whose
  // pieces were just found), or the wait for one scrolls into view. Opening
  // the thread jumps straight to the end rather than animating through it.
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: mounted.current ? 'smooth' : 'auto', block: 'end' })
    mounted.current = true
  }, [last, agent.sending])

  const lastAssistant = findLastIndex(turns, (t) => t.kind === 'assistant')
  const lastRoom = findLastIndex(
    turns,
    (t) => t.kind === 'assistant' && !!t.data.presentation?.room,
  )

  return (
    <div className="scroll-inner">
      <div className="thread" aria-live="polite">
        {turns.map((turn, index) => (
          <TurnView
            key={turn.id}
            turn={turn}
            latest={index === lastAssistant && index === turns.length - 1}
            currentRoom={index === lastRoom}
          />
        ))}
        {agent.sending && agent.activity !== 'detecting' && (
          <div className="row-bot">
            <Avatar name={config.assistantName} url={config.avatarUrl} size={28} />
            <div className="typing" role="status">
              <span className="dots">
                <i />
                <i />
                <i />
              </span>
              {WAITING[agent.activity ?? 'thinking']}
            </div>
          </div>
        )}
        <div ref={end} />
      </div>
    </div>
  )
}

function TurnView({ turn, latest, currentRoom }: { turn: Turn; latest: boolean; currentRoom: boolean }) {
  const { agent } = useWidget()
  switch (turn.kind) {
    case 'user':
      return (
        <div className="row-user">
          {turn.rejected ? (
            <div className="rejected">
              {turn.rejected.imageUrl ? <img src={turn.rejected.imageUrl} alt="" /> : <span className="ph" />}
              <div>
                <small>Not this one</small>
                <span>{turn.rejected.name}</span>
              </div>
            </div>
          ) : (
            <div className="bubble-user">{turn.text}</div>
          )}
        </div>
      )
    case 'assistant':
      return (
        <AssistantTurn
          data={turn.data}
          latest={latest}
          currentRoom={currentRoom}
          renderDropped={turn.renderDropped}
        />
      )
    case 'photo':
      return <PhotoTurn id={turn.id} url={turn.url} photo={turn.photo} />
    case 'error':
      return (
        <div className="bubble-error" role="alert">
          {turn.message}
          {turn.retry && (
            <div>
              <button onClick={() => agent.retry(turn.retry!)} disabled={agent.sending}>
                Try again
              </button>
            </div>
          )}
        </div>
      )
  }
}

function findLastIndex<T>(items: T[], test: (item: T) => boolean): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (test(items[index])) return index
  }
  return -1
}
