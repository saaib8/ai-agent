// What survives a page navigation on the merchant's site.
//
// A shopper moves between product pages while chatting, and each page load
// re-creates the iframe. The server keeps the conversation state (Redis, one
// hour idle TTL) but offers no "read history" endpoint, so the thread as the
// shopper saw it is kept here — per store, in this origin's storage — and
// dropped once it is older than the server session could be.
//
// Renders are data URLs of several megabytes and are never persisted; a photo
// turn points at an object URL that dies with the page, so it is dropped too.

import type { ChatResponse, PickView } from '../api/types'
import { newSessionId } from '../lib/session'
import type { Turn } from './types'

const TTL_MS = 55 * 60 * 1000
const MAX_TURNS = 40

interface Saved {
  savedAt: number
  sessionId: string
  turns: Turn[]
  picks: PickView[]
}

const key = (storeId: number) => `zory-agent:v1:${storeId}`

function strip(turn: Turn): Turn | null {
  if (turn.kind === 'photo') return null
  if (turn.kind === 'error') return { ...turn, retry: undefined }
  if (turn.kind === 'assistant' && turn.data.presentation?.render) {
    const data: ChatResponse = {
      ...turn.data,
      presentation: { ...turn.data.presentation, render: null },
    }
    return { ...turn, data, renderDropped: true }
  }
  return turn
}

export function loadThread(storeId: number): Saved {
  try {
    const raw = window.localStorage.getItem(key(storeId))
    if (raw) {
      const saved = JSON.parse(raw) as Saved
      if (
        typeof saved.sessionId === 'string' &&
        Array.isArray(saved.turns) &&
        Date.now() - saved.savedAt < TTL_MS
      ) {
        return { ...saved, picks: Array.isArray(saved.picks) ? saved.picks : [] }
      }
    }
  } catch {
    // Blocked or corrupt storage: start fresh rather than fail to open.
  }
  return { savedAt: Date.now(), sessionId: newSessionId(), turns: [], picks: [] }
}

export function saveThread(storeId: number, sessionId: string, turns: Turn[], picks: PickView[]) {
  const kept = turns.map(strip).filter((t): t is Turn => t !== null).slice(-MAX_TURNS)
  const saved: Saved = { savedAt: Date.now(), sessionId, turns: kept, picks }
  try {
    window.localStorage.setItem(key(storeId), JSON.stringify(saved))
  } catch {
    // Quota or private mode: the thread simply does not survive a reload.
  }
}

export function clearThread(storeId: number) {
  try {
    window.localStorage.removeItem(key(storeId))
  } catch {
    // Nothing was persisted.
  }
}
