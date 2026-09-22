import { useCallback, useEffect, useState } from 'react'
import { newSessionId } from '../lib/session'

export interface ConsoleConfig {
  /** "" means same-origin (routed through the Vite dev proxy to the backend). */
  apiBase: string
  storeId: number
  sessionId: string
  /** Opt-in optimistic-concurrency guard; off is correct for a plain chat box. */
  sendExpectedRevision: boolean
}

const STORAGE_KEY = 'zory-console-config'

const DEFAULTS: ConsoleConfig = {
  apiBase: '',
  storeId: 50,
  sessionId: newSessionId(),
  sendExpectedRevision: false,
}

function load(): ConsoleConfig {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULTS
    const parsed = JSON.parse(raw) as Partial<ConsoleConfig>
    return {
      apiBase: typeof parsed.apiBase === 'string' ? parsed.apiBase : DEFAULTS.apiBase,
      storeId: typeof parsed.storeId === 'number' ? parsed.storeId : DEFAULTS.storeId,
      sessionId: typeof parsed.sessionId === 'string' && parsed.sessionId ? parsed.sessionId : DEFAULTS.sessionId,
      sendExpectedRevision: Boolean(parsed.sendExpectedRevision),
    }
  } catch {
    return DEFAULTS
  }
}

export interface UseConfig {
  config: ConsoleConfig
  set: <K extends keyof ConsoleConfig>(key: K, value: ConsoleConfig[K]) => void
  rotateSession: () => void
}

export function useConfig(): UseConfig {
  const [config, setConfig] = useState<ConsoleConfig>(load)

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config))
  }, [config])

  const set = useCallback<UseConfig['set']>((key, value) => {
    setConfig((prev) => ({ ...prev, [key]: value }))
  }, [])

  const rotateSession = useCallback(() => {
    setConfig((prev) => ({ ...prev, sessionId: newSessionId() }))
  }, [])

  return { config, set, rotateSession }
}
