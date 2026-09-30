// The postMessage channel between this panel (inside the iframe) and the
// loader on the merchant's page (public/embed.js).
//
// Everything carries { source: 'zory-agent', v: 1 }. The loader only acts on
// messages whose origin and source window are this iframe; here we only accept
// messages from our direct parent. Nothing sensitive crosses in either
// direction: panel -> host is window state (open/expand/badge) and events for
// the merchant's own analytics; host -> panel is "open this tool" or "ask this".

const SOURCE = 'zory-agent'

export type HostCommand =
  | { type: 'state'; data: { open: boolean; expanded: boolean; mobile: boolean } }
  | { type: 'tool'; data: { tool: string } }
  | { type: 'ask'; data: { message: string } }

export type PanelMessage =
  | { type: 'ready' }
  | { type: 'close' }
  | { type: 'expand' }
  | { type: 'collapse' }
  | { type: 'badge'; data: { count: number } }
  | { type: 'event'; data: { name: string; payload?: unknown } }

export const inIframe = (() => {
  try {
    return window.self !== window.top
  } catch {
    return true
  }
})()

export function createBridge(hostOrigin: string | null) {
  const target = hostOrigin ?? '*'

  function post(message: PanelMessage) {
    if (!inIframe) return
    window.parent.postMessage({ source: SOURCE, v: 1, ...message }, target)
  }

  function listen(handler: (command: HostCommand) => void): () => void {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== window.parent) return
      if (hostOrigin && event.origin !== hostOrigin) return
      const msg = event.data as { source?: unknown; type?: unknown; data?: unknown } | null
      if (!msg || msg.source !== SOURCE || typeof msg.type !== 'string') return
      const data = (msg.data ?? {}) as Record<string, unknown>
      if (msg.type === 'state') {
        handler({
          type: 'state',
          data: { open: !!data.open, expanded: !!data.expanded, mobile: !!data.mobile },
        })
      } else if (msg.type === 'tool' && typeof data.tool === 'string') {
        handler({ type: 'tool', data: { tool: data.tool } })
      } else if (msg.type === 'ask' && typeof data.message === 'string') {
        handler({ type: 'ask', data: { message: data.message.slice(0, 1000) } })
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }

  return { post, listen }
}

export type Bridge = ReturnType<typeof createBridge>
