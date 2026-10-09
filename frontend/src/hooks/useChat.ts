import { useCallback, useEffect, useRef, useState } from 'react'
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
  LikedView,
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
  /** Abort the in-flight reply. A no-op when nothing is generating. */
  stop: () => void
  /** A reply is typing out progressively; Stop freezes it. */
  revealing: boolean
  /** How many characters of `revealTurnId`'s reply are shown. */
  revealedLen: number
  /** The turn whose reply is typing out, or null. */
  revealTurnId: string | null
  activity: Activity
  /** Revision the last committed turn produced; drives expected_session_revision. */
  revision: number | null
  /** The customer's picks, as the server last reported them. */
  picks: PickView[]
  /** A tick or untick is in flight: the session is being written. */
  picking: boolean
  /** Why the last tick was refused, in the server's words; cleared by the next. */
  picksError: string | null
  /** Their liked list as the server last reported it; null while unknown or
   *  when the buttons are switched off. */
  liked: LikedView[] | null
  /** ♡ taps not yet saved, by card (`list_revision:ordinal`): a like or its undo. */
  pendingLikes: Map<string, 'like' | 'unlike'>
  /** ♡ or its undo on a card. Queued while a reply or a tick is in flight,
   *  then saved - a like never races the session. A second tap on a card
   *  whose tap is still queued takes that tap back instead. */
  queueLike: (action: CardLikeAction, config: ConsoleConfig) => void
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

/** ♡ on a card, or its undo - always named by the card, which a queued tap
 *  still means after the liked list renumbers. */
export type CardLikeAction =
  | { kind: 'like'; ordinal: number; list_revision: number }
  | { kind: 'unlike'; ordinal: number; list_revision: number }

const cardKey = (action: CardLikeAction) => `${action.list_revision}:${action.ordinal}`

let counter = 0
const nextId = (): string => `t${++counter}`

// Typewriter pace for the reply reveal: readable, so it can be Stopped mid-way,
// yet the whole reply is on screen within REVEAL_MAX_MS however long it is -
// the reply is already validated, so the reveal is pacing, never a wait.
const REVEAL_TICK_MS = 28
const REVEAL_MAX_MS = 1500

export function useChat(): UseChat {
  const [turns, setTurns] = useState<Turn[]>([])
  const [sending, setSending] = useState(false)
  const [activity, setActivity] = useState<Activity>(null)
  const [revision, setRevision] = useState<number | null>(null)
  const [picks, setPicks] = useState<PickView[]>([])
  const [picking, setPicking] = useState(false)
  const [picksError, setPicksError] = useState<string | null>(null)
  const [liked, setLiked] = useState<LikedView[] | null>(null)
  const [likeQueue, setLikeQueue] = useState<{ action: CardLikeAction; config: ConsoleConfig }[]>(
    [],
  )
  // A ref as well as state: send() reads the latest revision without being
  // re-created on every commit.
  const revisionRef = useRef<number | null>(null)
  // Object URLs for shared photos, released on reset so they do not leak.
  const photoUrls = useRef<string[]>([])
  // The in-flight request, so a Stop button can abort it. The server cancels a
  // disconnected turn before it persists, so stopping leaves the conversation
  // exactly as it was - there is nothing to undo.
  const abortRef = useRef<AbortController | null>(null)
  // Progressive reveal: the reply is fully generated and validated server-side,
  // then typed onto the screen word by word. `revealedLen` is how many
  // characters of `revealTurnId`'s reply are shown; Stop freezes it there. The
  // customer therefore never sees an un-checked word - it is checked first, then
  // revealed.
  const [revealing, setRevealing] = useState(false)
  const [revealedLen, setRevealedLen] = useState(0)
  const [revealTurnId, setRevealTurnId] = useState<string | null>(null)
  const revealTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const revealTarget = useRef(0)

  const startReveal = (id: string, text: string) => {
    if (revealTimer.current) clearInterval(revealTimer.current)
    revealTimer.current = null
    if (!text) {
      setRevealTurnId(null)
      setRevealing(false)
      return
    }
    revealTarget.current = text.length
    const charsPerTick = Math.max(1, Math.ceil(text.length / (REVEAL_MAX_MS / REVEAL_TICK_MS)))
    setRevealTurnId(id)
    setRevealedLen(0)
    setRevealing(true)
    // The validated reply types out within REVEAL_MAX_MS (see above).
    revealTimer.current = setInterval(() => {
      setRevealedLen((n) => Math.min(n + charsPerTick, revealTarget.current))
    }, REVEAL_TICK_MS)
  }

  // Stop the typewriter once the whole validated reply is on screen.
  useEffect(() => {
    if (revealing && revealedLen >= revealTarget.current) {
      if (revealTimer.current) clearInterval(revealTimer.current)
      revealTimer.current = null
      setRevealing(false)
    }
  }, [revealing, revealedLen])

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
    if (data.liked) setLiked(data.liked)
    const id = nextId()
    setTurns((prev) => [...prev, { kind: 'assistant', id, data, selection }])
    // Type the (already-validated) reply out; the products follow once it lands.
    startReveal(id, data.response.message ?? '')
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
      const controller = new AbortController()
      abortRef.current = controller

      const result = await postChat(
        config.apiBase,
        {
          session_id: config.sessionId,
          store_id: config.storeId,
          message: text,
          ...expected(config),
          ...(opts?.bundle ? { bundle_action: opts.bundle } : {}),
          ...(opts?.search ? { search_action: opts.search } : {}),
          ...(opts?.product ? { product_action: opts.product } : {}),
        },
        controller.signal,
      )

      abortRef.current = null
      if (result.ok) commit(result.data)
      // Stopped: no reply came and nothing persisted. The message stays on
      // screen, the thread returns to idle, and no error is shown.
      else if (result.status !== 'aborted') fail(result.status, result.error)
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
      if (result.data.liked) setLiked(result.data.liked)
      return result.data
    }
    setPicksError(result.error.message)
    return null
  }, [])

  const queueLike = useCallback((action: CardLikeAction, config: ConsoleConfig) => {
    setLikeQueue((queue) => {
      const same = (queued: CardLikeAction) => cardKey(queued) === cardKey(action)
      return queue.some((q) => same(q.action))
        ? queue.filter((q) => !same(q.action))
        : [...queue, { action, config }]
    })
  }, [])

  // One queued like at a time, and only when nothing else is writing the session.
  useEffect(() => {
    if (sending || picking || likeQueue.length === 0) return
    const [next, ...rest] = likeQueue
    setLikeQueue(rest)
    void changePicks(next.action, next.config)
  }, [sending, picking, likeQueue, changePicks])

  const pendingLikes = new Map(likeQueue.map(({ action }) => [cardKey(action), action.kind]))

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

    // A stopped request is nothing to show, as for a message.
    if (result.ok) setPhoto(id, { status: 'ready', data: result.data, picked: [] })
    else if (result.status !== 'aborted') {
      setPhoto(id, { status: 'error', httpStatus: result.status, error: result.error })
    }
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
      else if (result.status !== 'aborted') fail(result.status, result.error)
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
      const controller = new AbortController()
      abortRef.current = controller

      const result = await postVisualize(
        config.apiBase,
        {
          session_id: config.sessionId,
          store_id: config.storeId,
          view,
          ...expected(config),
        },
        controller.signal,
      )

      abortRef.current = null
      if (result.ok) commit(result.data)
      else if (result.status !== 'aborted') fail(result.status, result.error)
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
      const controller = new AbortController()
      abortRef.current = controller

      const result = await postCatalogVisualize(
        config.apiBase,
        {
          session_id: config.sessionId,
          store_id: config.storeId,
          ...selection,
          view,
          ...expected(config),
        },
        controller.signal,
      )

      abortRef.current = null
      if (result.ok) commit(result.data, selection)
      else if (result.status !== 'aborted') fail(result.status, result.error)
      setActivity(null)
      setSending(false)
    },
    [],
  )

  const stop = useCallback(() => {
    // Mid-generation: abort the request; the server discards the disconnected
    // turn, so the conversation does not advance. Mid-reveal: the reply is
    // already validated and saved, so just freeze the text where it is.
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    } else if (revealTimer.current) {
      clearInterval(revealTimer.current)
      revealTimer.current = null
      setRevealing(false)
    }
  }, [])

  const reset = useCallback(() => {
    photoUrls.current.forEach((url) => URL.revokeObjectURL(url))
    photoUrls.current = []
    if (revealTimer.current) clearInterval(revealTimer.current)
    revealTimer.current = null
    setRevealing(false)
    setRevealedLen(0)
    setRevealTurnId(null)
    setTurns([])
    setRevision(null)
    setPicks([])
    setPicksError(null)
    setLiked(null)
    setLikeQueue([])
    revisionRef.current = null
  }, [])

  return {
    turns,
    sending,
    stop,
    revealing,
    revealedLen,
    revealTurnId,
    activity,
    revision,
    picks,
    picking,
    picksError,
    liked,
    pendingLikes,
    queueLike,
    send,
    changePicks,
    uploadPhoto,
    pickObject,
    visualize,
    visualizeSelection,
    reset,
  }
}
