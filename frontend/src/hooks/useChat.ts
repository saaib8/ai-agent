import { useCallback, useRef, useState } from 'react'
import { postChat } from '../api/client'
import type { ChatResponse, ErrorBody } from '../api/types'
import type { ConsoleConfig } from './useConfig'

export type Turn =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; data: ChatResponse }
  | { kind: 'error'; id: string; status: number | 'network'; error: ErrorBody }

export interface UseChat {
  turns: Turn[]
  sending: boolean
  /** Revision the last committed turn produced; drives expected_session_revision. */
  revision: number | null
  send: (message: string, config: ConsoleConfig) => Promise<void>
  reset: () => void
}

let counter = 0
const nextId = (): string => `t${++counter}`

export function useChat(): UseChat {
  const [turns, setTurns] = useState<Turn[]>([])
  const [sending, setSending] = useState(false)
  const [revision, setRevision] = useState<number | null>(null)
  // A ref as well as state: send() reads the latest revision without being
  // re-created on every commit.
  const revisionRef = useRef<number | null>(null)

  const send = useCallback(async (message: string, config: ConsoleConfig) => {
    const text = message.trim()
    if (!text) return

    setTurns((prev) => [...prev, { kind: 'user', id: nextId(), text }])
    setSending(true)

    const result = await postChat(config.apiBase, {
      session_id: config.sessionId,
      store_id: config.storeId,
      message: text,
      ...(config.sendExpectedRevision && revisionRef.current != null
        ? { expected_session_revision: revisionRef.current }
        : {}),
    })

    if (result.ok) {
      revisionRef.current = result.data.session_revision
      setRevision(result.data.session_revision)
      setTurns((prev) => [...prev, { kind: 'assistant', id: nextId(), data: result.data }])
    } else {
      setTurns((prev) => [
        ...prev,
        { kind: 'error', id: nextId(), status: result.status, error: result.error },
      ])
    }
    setSending(false)
  }, [])

  const reset = useCallback(() => {
    setTurns([])
    setRevision(null)
    revisionRef.current = null
  }, [])

  return { turns, sending, revision, send, reset }
}
