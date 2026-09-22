import type {
  ChatRequest,
  ChatResponse,
  ErrorBody,
  HealthResponse,
} from './types'

// A discriminated result so callers handle failure explicitly rather than
// catching. The backend never leaks stack traces — an error is always a typed
// { code, message, trace_id } body — and network failures are normalised into
// the same shape here.

export type ChatResult =
  | { ok: true; data: ChatResponse }
  | { ok: false; status: number | 'network'; error: ErrorBody }

export type HealthResult =
  | { ok: true; data: HealthResponse }
  | { ok: false; error: string }

/** Trim a trailing slash so `${base}/v1/chat` never doubles up. Empty base ("")
 *  means same-origin, which is routed through the Vite dev proxy. */
function normaliseBase(base: string): string {
  return base.trim().replace(/\/+$/, '')
}

const NETWORK_ERROR = (base: string): ErrorBody => ({
  code: 'network_error',
  message:
    `Could not reach the service at ${base || 'the dev proxy'}. Check that the ` +
    `FastAPI service is running and the API base URL / proxy target is correct.`,
  trace_id: null,
})

export async function postChat(base: string, body: ChatRequest): Promise<ChatResult> {
  const root = normaliseBase(base)
  let res: Response
  try {
    res = await fetch(`${root}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    return { ok: false, status: 'network', error: NETWORK_ERROR(root) }
  }

  const payload: unknown = await res.json().catch(() => null)

  if (res.ok && payload && typeof payload === 'object' && 'response' in payload) {
    return { ok: true, data: payload as ChatResponse }
  }

  const error =
    payload && typeof payload === 'object' && 'error' in payload
      ? (payload as { error: ErrorBody }).error
      : { code: `http_${res.status}`, message: res.statusText || 'Request failed', trace_id: null }

  return { ok: false, status: res.status, error }
}

export async function getHealth(base: string): Promise<HealthResult> {
  const root = normaliseBase(base)
  try {
    const res = await fetch(`${root}/health`, { method: 'GET' })
    const data: unknown = await res.json().catch(() => null)
    if (data && typeof data === 'object' && 'status' in data) {
      return { ok: true, data: data as HealthResponse }
    }
    return { ok: false, error: `Unexpected /health response (HTTP ${res.status})` }
  } catch {
    return { ok: false, error: 'unreachable' }
  }
}
