import { useCallback, useRef, useState } from 'react'
import {
  postCatalogVisualize,
  postChat,
  postFinderPhoto,
  postFinderPick,
  postPicks,
  postVisualize,
} from '../api/client'
import type {
  BundleAction,
  CatalogSelection,
  ChatResponse,
  ErrorBody,
  FinderObject,
  FinderPhotoResponse,
  PickAction,
  PickView,
  PicksResponse,
  ProductAction,
  RenderView,
  SearchAction,
} from '../api/types'
import type { ConsoleConfig } from './useConfig'

/** A product a "Not this one" tap dismissed, shown on the user's turn so the
 *  thread makes clear what was passed on rather than a bare line of text. */
export interface RejectedRef {
  name: string
  imageUrl: string
  /** The heading above it - "Not this one", in the conversation's language. */
  label?: string
}

/** A photo the customer shared, as it moves from uploading to pickable. */
export type PhotoState =
  | { status: 'detecting' }
  | { status: 'ready'; data: FinderPhotoResponse; picked: number[] }
  | { status: 'error'; httpStatus: number | 'network'; error: ErrorBody }

export type Turn =
  | { kind: 'user'; id: string; text: string; rejected?: RejectedRef }
  | { kind: 'assistant'; id: string; data: ChatResponse; selection?: CatalogSelection }
  | { kind: 'error'; id: string; status: number | 'network'; error: ErrorBody }
  | { kind: 'photo'; id: string; url: string; photo: PhotoState }

/** A long-running turn the waiting indicator should name. */
export type Activity = 'rendering' | null

/** Options a message can carry: a structured action, and how to show it. */
export interface SendOptions {
  bundle?: BundleAction
  search?: SearchAction
  product?: ProductAction
  rejected?: RejectedRef
}

export interface UseChat {
  turns: Turn[]
  sending: boolean
  activity: Activity
  /** Revision the last committed turn produced; drives expected_session_revision. */
  revision: number | null
  /** The customer's picks, as the server last reported them. */
  picks: PickView[]
  /** A tick or untick is in flight: the session is being written. */
  picking: boolean
  /** Why the last tick was refused, in the server's words; cleared by the next. */
  picksError: string | null
  send: (message: string, config: ConsoleConfig, opts?: SendOptions) => Promise<void>
  /** Tick a card or untick a pick. Silent: no chat turn. */
  /** Tick or untick. Resolves to the server's answer, or null on failure. */
  changePicks: (action: PickAction, config: ConsoleConfig) => Promise<PicksResponse | null>
  /** Share a photo: detect what is in it. Commits nothing to the session. */
  uploadPhoto: (file: File, config: ConsoleConfig) => Promise<void>
  /** Pick an object in a shared photo. A committed turn, like a message. */
  pickObject: (
    photoTurnId: string,
    imageId: string,
    object: FinderObject,
    config: ConsoleConfig,
  ) => Promise<void>
  /** Render the room package from a camera view. A committed turn. */
  visualize: (view: RenderView, viewLabel: string, config: ConsoleConfig) => Promise<void>
  /** Render pieces picked from the catalogue. A committed turn, which keeps
   *  the selection so it can be drawn again or edited. */
  visualizeSelection: (
    selection: CatalogSelection,
    view: RenderView,
    summary: string,
    config: ConsoleConfig,
  ) => Promise<void>
  reset: () => void
}

let counter = 0
const nextId = (): string => `t${++counter}`

export function useChat(): UseChat {
  const [turns, setTurns] = useState<Turn[]>([])
  const [sending, setSending] = useState(false)
  const [activity, setActivity] = useState<Activity>(null)
  const [revision, setRevision] = useState<number | null>(null)
  const [picks, setPicks] = useState<PickView[]>([])
  const [picking, setPicking] = useState(false)
  const [picksError, setPicksError] = useState<string | null>(null)
  // A ref as well as state: send() reads the latest revision without being
  // re-created on every commit.
  const revisionRef = useRef<number | null>(null)
  // Object URLs for shared photos, released on reset so they do not leak.
  const photoUrls = useRef<string[]>([])

  const expected = (config: ConsoleConfig) =>
    config.sendExpectedRevision && revisionRef.current != null
      ? { expected_session_revision: revisionRef.current }
      : {}

  const commit = (data: ChatResponse, selection?: CatalogSelection) => {
    revisionRef.current = data.session_revision
    setRevision(data.session_revision)
    // The server is the source of truth for what is picked; absent means the
    // turn did not report picks, so the tray stays as it is.
    if (data.picks) setPicks(data.picks)
    setTurns((prev) => [...prev, { kind: 'assistant', id: nextId(), data, selection }])
  }

  const fail = (status: number | 'network', error: ErrorBody) =>
    setTurns((prev) => [...prev, { kind: 'error', id: nextId(), status, error }])

  const send = useCallback(
    async (message: string, config: ConsoleConfig, opts?: SendOptions) => {
      const text = message.trim()
      if (!text) return

      setTurns((prev) => [
        ...prev,
        { kind: 'user', id: nextId(), text, rejected: opts?.rejected },
      ])
      setSending(true)

      const result = await postChat(config.apiBase, {
        session_id: config.sessionId,
        store_id: config.storeId,
        message: text,
        ...expected(config),
        ...(opts?.bundle ? { bundle_action: opts.bundle } : {}),
        ...(opts?.search ? { search_action: opts.search } : {}),
        ...(opts?.product ? { product_action: opts.product } : {}),
      })

      if (result.ok) commit(result.data)
      else fail(result.status, result.error)
      setSending(false)
    },
    [],
  )

  const changePicks = useCallback(async (action: PickAction, config: ConsoleConfig) => {
    setPicking(true)
    setPicksError(null)
    const result = await postPicks(config.apiBase, {
      session_id: config.sessionId,
      store_id: config.storeId,
      action,
      ...expected(config),
    })
    setPicking(false)
    if (result.ok) {
      revisionRef.current = result.data.session_revision
      setRevision(result.data.session_revision)
      setPicks(result.data.picks)
      return result.data
    }
    setPicksError(result.error.message)
    return null
  }, [])

  const setPhoto = (id: string, photo: PhotoState) =>
    setTurns((prev) => prev.map((t) => (t.id === id && t.kind === 'photo' ? { ...t, photo } : t)))

  const uploadPhoto = useCallback(async (file: File, config: ConsoleConfig) => {
    const id = nextId()
    const url = URL.createObjectURL(file)
    photoUrls.current.push(url)
    setTurns((prev) => [...prev, { kind: 'photo', id, url, photo: { status: 'detecting' } }])
    setSending(true)

    const result = await postFinderPhoto(config.apiBase, {
      sessionId: config.sessionId,
      storeId: config.storeId,
      file,
    })

    setPhoto(
      id,
      result.ok
        ? { status: 'ready', data: result.data, picked: [] }
        : { status: 'error', httpStatus: result.status, error: result.error },
    )
    setSending(false)
  }, [])

  const pickObject = useCallback(
    async (photoTurnId: string, imageId: string, object: FinderObject, config: ConsoleConfig) => {
      // Mark the object picked on the photo, so the customer can see which
      // items they have already searched for.
      setTurns((prev) =>
        prev.map((t) =>
          t.id === photoTurnId && t.kind === 'photo' && t.photo.status === 'ready'
            ? {
                ...t,
                photo: {
                  ...t.photo,
                  picked: t.photo.picked.includes(object.object_id)
                    ? t.photo.picked
                    : [...t.photo.picked, object.object_id],
                },
              }
            : t,
        ),
      )
      setTurns((prev) => [
        ...prev,
        { kind: 'user', id: nextId(), text: `Find products like the ${object.display_label}` },
      ])
      setSending(true)

      const result = await postFinderPick(config.apiBase, {
        session_id: config.sessionId,
        store_id: config.storeId,
        image_id: imageId,
        object_id: object.object_id,
        ...expected(config),
      })

      if (result.ok) commit(result.data)
      else fail(result.status, result.error)
      setSending(false)
    },
    [],
  )

  const visualize = useCallback(
    async (view: RenderView, viewLabel: string, config: ConsoleConfig) => {
      setTurns((prev) => [
        ...prev,
        { kind: 'user', id: nextId(), text: `Visualize my room — ${viewLabel.toLowerCase()} view` },
      ])
      setSending(true)
      setActivity('rendering')

      const result = await postVisualize(config.apiBase, {
        session_id: config.sessionId,
        store_id: config.storeId,
        view,
        ...expected(config),
      })

      if (result.ok) commit(result.data)
      else fail(result.status, result.error)
      setActivity(null)
      setSending(false)
    },
    [],
  )

  const visualizeSelection = useCallback(
    async (selection: CatalogSelection, view: RenderView, summary: string, config: ConsoleConfig) => {
      setTurns((prev) => [...prev, { kind: 'user', id: nextId(), text: summary }])
      setSending(true)
      setActivity('rendering')

      const result = await postCatalogVisualize(config.apiBase, {
        session_id: config.sessionId,
        store_id: config.storeId,
        ...selection,
        view,
        ...expected(config),
      })

      if (result.ok) commit(result.data, selection)
      else fail(result.status, result.error)
      setActivity(null)
      setSending(false)
    },
    [],
  )

  const reset = useCallback(() => {
    photoUrls.current.forEach((url) => URL.revokeObjectURL(url))
    photoUrls.current = []
    setTurns([])
    setRevision(null)
    setPicks([])
    setPicksError(null)
    revisionRef.current = null
  }, [])

  return {
    turns,
    sending,
    activity,
    revision,
    picks,
    picking,
    picksError,
    send,
    changePicks,
    uploadPhoto,
    pickObject,
    visualize,
    visualizeSelection,
    reset,
  }
}
