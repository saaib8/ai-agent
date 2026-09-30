import type {
  BundleAction,
  ChatResponse,
  ErrorBody,
  FinderPhotoResponse,
  ProductAction,
  SearchAction,
} from '../api/types'

/** A product a "Not this one" tap dismissed, shown on the shopper's turn. */
export interface RejectedRef {
  name: string
  imageUrl: string
}

/** A shared photo, from uploading to pickable. */
export type PhotoState =
  | { status: 'detecting' }
  | { status: 'ready'; data: FinderPhotoResponse; picked: number[] }
  | { status: 'error'; message: string }

/** What a failed message was, so "Try again" can send it exactly as before. */
export interface RetryRequest {
  text: string
  opts?: SendOptions
}

export type Turn =
  | { kind: 'user'; id: string; text: string; rejected?: RejectedRef }
  | { kind: 'assistant'; id: string; data: ChatResponse; renderDropped?: boolean }
  | { kind: 'photo'; id: string; url: string; photo: PhotoState }
  | { kind: 'error'; id: string; message: string; retry?: RetryRequest }

export interface SendOptions {
  bundle?: BundleAction
  search?: SearchAction
  product?: ProductAction
  rejected?: RejectedRef
  /** Names the wait, e.g. a render takes far longer than a reply. */
  activity?: Activity
}

export type Activity = 'thinking' | 'rendering' | 'detecting'

/** A room piece being swapped: the shopper is choosing its replacement. */
export interface SwapContext {
  bundleOrdinal: number
  role: string
}

export type ErrorLike = { status: number | 'network'; error: ErrorBody }
