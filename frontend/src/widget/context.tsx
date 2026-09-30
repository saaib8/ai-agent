import { createContext, useContext } from 'react'
import type { CatalogFacets } from '../api/types'
import type { Bridge } from './bridge'
import type { WidgetConfig } from './config'
import type { ViewName } from './tools'
import type { Agent } from './useAgent'

export interface WidgetContextValue {
  config: WidgetConfig
  agent: Agent
  bridge: Bridge
  /** The store's live filter values, or null until loaded (or if unreachable). */
  facets: CatalogFacets | null
  /** Sidebar layout: the host expanded the panel, or the iframe is wide. */
  expanded: boolean
  view: ViewName
  go: (view: ViewName) => void
  /** Send a message and show the conversation. */
  ask: (message: string) => void
  /** Open the device's file picker for a room or furniture photo. */
  pickPhoto: () => void
}

export const WidgetContext = createContext<WidgetContextValue | null>(null)

export function useWidget(): WidgetContextValue {
  const value = useContext(WidgetContext)
  if (!value) throw new Error('useWidget must be used inside the widget provider')
  return value
}
