import { useCallback, useEffect, useState } from 'react'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'

export type HealthState =
  | { status: 'unknown' }
  | { status: 'checking' }
  | { status: 'ok' | 'degraded'; report: HealthResponse }
  | { status: 'unreachable'; detail: string }

export interface UseHealth {
  health: HealthState
  check: () => Promise<void>
}

export function useHealth(apiBase: string): UseHealth {
  const [health, setHealth] = useState<HealthState>({ status: 'unknown' })

  const check = useCallback(async () => {
    setHealth({ status: 'checking' })
    const result = await getHealth(apiBase)
    if (result.ok) {
      setHealth({ status: result.data.status === 'ok' ? 'ok' : 'degraded', report: result.data })
    } else {
      setHealth({ status: 'unreachable', detail: result.error })
    }
  }, [apiBase])

  // Check once on mount and whenever the target changes.
  useEffect(() => {
    void check()
  }, [check])

  return { health, check }
}
