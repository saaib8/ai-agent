// What one installation of the widget is: read once from the iframe URL the
// loader built from the merchant's snippet. Nothing here is trusted for
// authority — the store id only scopes which catalog the agent reads, exactly
// as it does for every other caller of the V1 API (no auth exists in V1).

export interface WidgetConfig {
  storeId: number
  /** What the assistant is called in the header and launcher. */
  assistantName: string
  /** The merchant's name, as in "Your Oakline assistant". */
  storeName: string
  /** Brand colour for highlights; a validated hex or the default teal. */
  accent: string
  /** An https image for the assistant's face, or null for the monogram. */
  avatarUrl: string | null
  /** The embedding page's origin, used as the postMessage target. */
  hostOrigin: string | null
  /** Where /v1 lives: the snippet's `data-api` origin (e.g. the deployed
   *  https://stage.ai-agent.zory.ai), or "" for same-origin /v1 — right when
   *  the widget is served by the same host as the API. A cross-origin API
   *  must list the widget's origin in its CORS allowlist. */
  apiBase: string
}

const HEX = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/
const DEFAULT_ACCENT = '#3e8da8'

function text(value: string | null, fallback: string, max = 40): string {
  const trimmed = (value ?? '').trim()
  return trimmed ? trimmed.slice(0, max) : fallback
}

function httpsUrl(value: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.hostname === 'localhost' ? url.toString() : null
  } catch {
    return null
  }
}

function origin(value: string | null): string | null {
  if (!value) return null
  try {
    return new URL(value).origin
  } catch {
    return null
  }
}

/** An API base is an origin only — https, or http on localhost — so a
 *  snippet can never point the widget at a path or another scheme. */
function apiOrigin(value: string | null): string {
  if (!value) return ''
  try {
    const url = new URL(value)
    const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
    return url.protocol === 'https:' || (local && url.protocol === 'http:') ? url.origin : ''
  } catch {
    return ''
  }
}

export function readConfig(search: string = window.location.search): WidgetConfig | null {
  const params = new URLSearchParams(search)
  const store = Number(params.get('store'))
  if (!Number.isInteger(store) || store < 1) return null
  const accent = params.get('accent') ?? ''
  return {
    storeId: store,
    assistantName: text(params.get('name'), 'Nora', 24),
    storeName: text(params.get('storeName'), 'store', 40),
    accent: HEX.test(accent) ? accent : DEFAULT_ACCENT,
    avatarUrl: httpsUrl(params.get('avatar')),
    hostOrigin: origin(params.get('host')),
    apiBase: apiOrigin(params.get('api')),
  }
}
