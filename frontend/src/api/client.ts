import type {
  ChatRequest,
  ChatResponse,
  ErrorBody,
  FinderPhotoResponse,
  FinderPickRequest,
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
  return postJson<ChatResponse>(base, '/v1/chat', body, isChatResponse)
}

// ── Furniture Finder ─────────────────────────────────────────────────────────

export type PhotoResult =
  | { ok: true; data: FinderPhotoResponse }
  | { ok: false; status: number | 'network'; error: ErrorBody }

/** Upload a photo; the answer lists the objects in it the catalog can match. */
export async function postFinderPhoto(
  base: string,
  fields: { sessionId: string; storeId: number; file: File },
): Promise<PhotoResult> {
  const form = new FormData()
  form.append('session_id', fields.sessionId)
  form.append('store_id', String(fields.storeId))
  form.append('image', fields.file)
  const root = normaliseBase(base)
  // No Content-Type header: the browser sets the multipart boundary itself.
  return send<FinderPhotoResponse>(
    root,
    () => fetch(`${root}/v1/furniture-finder/photos`, { method: 'POST', body: form }),
    (payload) => typeof payload === 'object' && payload !== null && 'image_id' in payload,
  )
}

/** Pick one object. The answer is a chat turn, exactly like postChat's. */
export async function postFinderPick(base: string, body: FinderPickRequest): Promise<ChatResult> {
  return postJson<ChatResponse>(base, '/v1/furniture-finder/picks', body, isChatResponse)
}

// ── transport ────────────────────────────────────────────────────────────────

type Result<T> =
  | { ok: true; data: T }
  | { ok: false; status: number | 'network'; error: ErrorBody }

function isChatResponse(payload: unknown): boolean {
  return typeof payload === 'object' && payload !== null && 'response' in payload
}

function postJson<T>(
  base: string,
  path: string,
  body: unknown,
  accept: (payload: unknown) => boolean,
): Promise<Result<T>> {
  const root = normaliseBase(base)
  return send<T>(
    root,
    () =>
      fetch(`${root}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    accept,
  )
}

async function send<T>(
  root: string,
  request: () => Promise<Response>,
  accept: (payload: unknown) => boolean,
): Promise<Result<T>> {
  let res: Response
  try {
    res = await request()
  } catch {
    return { ok: false, status: 'network', error: NETWORK_ERROR(root) }
  }

  const payload: unknown = await res.json().catch(() => null)

  if (res.ok && accept(payload)) {
    return { ok: true, data: payload as T }
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
