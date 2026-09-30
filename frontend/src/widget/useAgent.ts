import { useCallback, useEffect, useRef, useState } from 'react'
import {
  postChat,
  postFinderPhoto,
  postFinderPick,
  postPicks,
  postVisualize,
} from '../api/client'
import type {
  BriefAnswerAction,
  ChatResponse,
  FinderObject,
  GroundedProduct,
  PickAction,
  PickView,
  PicksResponse,
  RenderView,
  ReplyChoice,
} from '../api/types'
import { newSessionId } from '../lib/session'
import type { WidgetConfig } from './config'
import { clearThread, loadThread, saveThread } from './storage'
import type { Activity, ErrorLike, RetryRequest, SendOptions, SwapContext, Turn } from './types'

let counter = 0
const nextId = (): string => `w${Date.now().toString(36)}${++counter}`

/** What the shopper reads when something goes wrong. The backend's own
 *  messages are customer-safe, so a refusal (4xx) is shown as written; an
 *  outage is worded here, without codes or trace ids. */
function friendly({ status, error }: ErrorLike): string {
  if (status === 'network') {
    return "I can't reach the store's assistant right now. Check your connection and try again."
  }
  if (status === 429) return "I'm getting a lot of questions right now. Please try again in a moment."
  if (status >= 500) return "Something went wrong on my side. Please try again in a moment."
  if (status === 409) return 'This conversation moved on in another window. Please try again.'
  return error.message || "I couldn't do that. Please try again."
}

export interface Agent {
  sessionId: string
  turns: Turn[]
  sending: boolean
  activity: Activity | null
  picks: PickView[]
  picking: boolean
  notice: string | null
  swap: SwapContext | null
  /** Pick numbers queued for a side-by-side, at most one waiting. */
  comparing: number[]
  send: (text: string, opts?: SendOptions) => Promise<void>
  retry: (request: RetryRequest) => void
  choose: (choice: ReplyChoice) => void
  submitBrief: (answer: BriefAnswerAction, summary: string) => void
  toggleBasket: (product: GroundedProduct, listRevision: number) => Promise<void>
  toggleCompare: (product: GroundedProduct, listRevision: number) => Promise<void>
  removePick: (pick: PickView) => void
  goesWith: (pick: PickView) => void
  compare: (first: PickView, second: PickView) => void
  showMore: () => void
  exclude: (product: GroundedProduct) => void
  startSwap: (bundleOrdinal: number, role: string) => void
  chooseAlternative: (alternativeOrdinal: number) => void
  cancelSwap: () => void
  uploadPhoto: (file: File) => Promise<void>
  pickObject: (photoTurnId: string, imageId: string, object: FinderObject) => Promise<void>
  visualize: (view: RenderView, viewLabel: string) => void
  newChat: () => void
  /** A short-lived message for the shopper, e.g. "Added to your basket". */
  flash: (message: string) => void
  dismissNotice: () => void
}

export function useAgent(config: WidgetConfig): Agent {
  const initial = useRef(loadThread(config.storeId)).current
  const [sessionId, setSessionId] = useState(initial.sessionId)
  const [turns, setTurns] = useState<Turn[]>(initial.turns)
  const [picks, setPicks] = useState<PickView[]>(initial.picks)
  const [sending, setSending] = useState(false)
  const [activity, setActivity] = useState<Activity | null>(null)
  const [picking, setPicking] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [swap, setSwap] = useState<SwapContext | null>(null)
  const [comparing, setComparing] = useState<number[]>([])
  // Refs mirror what async handlers must read without being re-created.
  const busy = useRef(false)
  const sessionRef = useRef(sessionId)
  const picksRef = useRef(picks)
  const photoUrls = useRef<string[]>([])
  picksRef.current = picks
  sessionRef.current = sessionId

  useEffect(() => {
    saveThread(config.storeId, sessionId, turns, picks)
  }, [config.storeId, sessionId, turns, picks])

  useEffect(
    () => () => photoUrls.current.forEach((url) => URL.revokeObjectURL(url)),
    [],
  )

  const base = { session_id: sessionId, store_id: config.storeId }

  const commit = (data: ChatResponse) => {
    if (data.picks) setPicks(data.picks)
    setTurns((prev) => [...prev, { kind: 'assistant', id: nextId(), data }])
  }

  /** Retrying helps only when the failure was the network or the service —
   *  a refusal (an unknown store, a malformed request) would fail the same way. */
  const fail = (failure: ErrorLike, retry?: RetryRequest) => {
    const transient = failure.status === 'network' || failure.status === 429 || failure.status >= 500
    setTurns((prev) => [
      ...prev,
      { kind: 'error', id: nextId(), message: friendly(failure), retry: transient ? retry : undefined },
    ])
  }

  const send = useCallback(
    async (text: string, opts?: SendOptions) => {
      const message = text.trim().slice(0, 1000)
      if (!message || busy.current) return
      busy.current = true
      setTurns((prev) => [...prev, { kind: 'user', id: nextId(), text: message, rejected: opts?.rejected }])
      setSending(true)
      setActivity(opts?.activity ?? 'thinking')

      const result = await postChat(config.apiBase, {
        session_id: sessionRef.current,
        store_id: config.storeId,
        message,
        ...(opts?.bundle ? { bundle_action: opts.bundle } : {}),
        ...(opts?.search ? { search_action: opts.search } : {}),
        ...(opts?.product ? { product_action: opts.product } : {}),
      })

      if (result.ok) commit(result.data)
      else fail(result, { text: message, opts })
      setSending(false)
      setActivity(null)
      busy.current = false
    },
    // commit/fail only call state setters, which are stable.
    [config.apiBase, config.storeId],
  )

  const retry = useCallback(
    (request: RetryRequest) => {
      // The failed turn and its question are replaced by the new attempt.
      setTurns((prev) => {
        const last = prev[prev.length - 1]
        const beforeLast = prev[prev.length - 2]
        if (last?.kind === 'error' && beforeLast?.kind === 'user') return prev.slice(0, -2)
        if (last?.kind === 'error') return prev.slice(0, -1)
        return prev
      })
      void send(request.text, request.opts)
    },
    [send],
  )

  const changePicks = async (action: PickAction): Promise<PicksResponse | null> => {
    setPicking(true)
    const result = await postPicks(config.apiBase, { ...base, action })
    setPicking(false)
    if (result.ok) {
      setPicks(result.data.picks)
      return result.data
    }
    setNotice(friendly(result))
    return null
  }

  /** The pick showing this card of this list, if the shopper has it already. */
  const pickFor = (product: GroundedProduct, listRevision: number, list = picksRef.current) =>
    list.find((p) =>
      (p.positions ?? []).some(
        (pos) => pos.list_revision === listRevision && pos.ordinal === product.presented_ordinal,
      ),
    )

  const toggleBasket = async (product: GroundedProduct, listRevision: number) => {
    const ordinal = product.presented_ordinal
    if (busy.current || picking || ordinal == null) return
    const already = pickFor(product, listRevision)
    if (already) {
      await changePicks({ kind: 'deselect', pick: already.pick })
      setComparing((queue) => queue.filter((n) => n !== already.pick))
      return
    }
    const saved = await changePicks({ kind: 'select', ordinal, list_revision: listRevision })
    if (!saved) return
    setNotice(`Added to your basket`)
    // The first pick of its kind: show what goes with it, as its own turn.
    const chosen = saved.picks.find((p) => p.pick === saved.goes_with)
    if (chosen) {
      void send(`I like the ${chosen.name_english}`, {
        product: { kind: 'goes_with', pick: chosen.pick },
      })
    }
  }

  const compare = useCallback(
    (first: PickView, second: PickView) => {
      setComparing([])
      void send(`Compare the ${first.name_english} and the ${second.name_english}`, {
        product: { kind: 'compare', picks: [first.pick, second.pick] },
      })
    },
    [send],
  )

  /** Mark a card for a side-by-side. Comparison is of two picks, so a card
   *  not yet picked is picked first (silently: no "goes with" turn); the
   *  second card marked runs the comparison. */
  const toggleCompare = async (product: GroundedProduct, listRevision: number) => {
    const ordinal = product.presented_ordinal
    if (busy.current || picking || ordinal == null) return
    let current = picksRef.current
    let pick = pickFor(product, listRevision, current)
    if (!pick) {
      const saved = await changePicks({ kind: 'select', ordinal, list_revision: listRevision })
      if (!saved) return
      current = saved.picks
      pick = pickFor(product, listRevision, current)
      if (!pick) return
    }
    const chosen = pick.pick
    if (comparing.includes(chosen)) {
      setComparing((queue) => queue.filter((n) => n !== chosen))
      return
    }
    const queue = [...comparing.filter((n) => current.some((p) => p.pick === n)), chosen].slice(-2)
    const [first, second] = queue.map((n) => current.find((p) => p.pick === n))
    if (first && second) {
      compare(first, second)
      return
    }
    setComparing(queue)
    setNotice('Choose one more piece to compare')
  }

  const removePick = (pick: PickView) => {
    if (busy.current || picking) return
    setComparing((queue) => queue.filter((n) => n !== pick.pick))
    void changePicks({ kind: 'deselect', pick: pick.pick })
  }

  const goesWith = (pick: PickView) =>
    void send(`What goes with the ${pick.name_english}?`, {
      product: { kind: 'goes_with', pick: pick.pick },
    })

  const choose = (choice: ReplyChoice) =>
    void send(choice.value, choice.product_action ? { product: choice.product_action } : undefined)

  const submitBrief = (answer: BriefAnswerAction, summary: string) =>
    void send(summary, { search: answer })

  const showMore = () => {
    setSwap(null)
    void send('Show me different options', { search: { kind: 'more_options' } })
  }

  const exclude = (product: GroundedProduct) => {
    if (product.presented_ordinal == null) return
    setSwap(null)
    void send(`Not this one — the ${product.name_english}`, {
      search: { kind: 'exclude', ordinal: product.presented_ordinal },
      rejected: { name: product.name_english, imageUrl: product.image_url },
    })
  }

  const startSwap = (bundleOrdinal: number, role: string) => {
    if (busy.current) return
    setSwap({ bundleOrdinal, role })
    void send(`Show me other ${role} options`, {
      bundle: { kind: 'list_alternatives', bundle_ordinal: bundleOrdinal },
    })
  }

  const chooseAlternative = (alternativeOrdinal: number) => {
    if (!swap || busy.current) return
    const current = swap
    setSwap(null)
    void send(`Use option ${alternativeOrdinal} for the ${current.role}`, {
      bundle: {
        kind: 'swap',
        bundle_ordinal: current.bundleOrdinal,
        alternative_ordinal: alternativeOrdinal,
      },
    })
  }

  const replaceTurn = (id: string, turn: Turn) =>
    setTurns((prev) => prev.map((t) => (t.id === id ? turn : t)))

  const uploadPhoto = async (file: File) => {
    if (busy.current) return
    busy.current = true
    const id = nextId()
    const url = URL.createObjectURL(file)
    photoUrls.current.push(url)
    setTurns((prev) => [...prev, { kind: 'photo', id, url, photo: { status: 'detecting' } }])
    setSending(true)
    setActivity('detecting')
    const result = await postFinderPhoto(config.apiBase, {
      sessionId: sessionRef.current,
      storeId: config.storeId,
      file,
    })
    replaceTurn(id, {
      kind: 'photo',
      id,
      url,
      photo: result.ok
        ? { status: 'ready', data: result.data, picked: [] }
        : { status: 'error', message: friendly(result) },
    })
    setSending(false)
    setActivity(null)
    busy.current = false
  }

  const pickObject = async (photoTurnId: string, imageId: string, object: FinderObject) => {
    if (busy.current) return
    busy.current = true
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
    const text = `Find pieces like the ${object.display_label}`
    setTurns((prev) => [...prev, { kind: 'user', id: nextId(), text }])
    setSending(true)
    setActivity('thinking')
    const result = await postFinderPick(config.apiBase, {
      ...base,
      image_id: imageId,
      object_id: object.object_id,
    })
    if (result.ok) commit(result.data)
    else fail(result)
    setSending(false)
    setActivity(null)
    busy.current = false
  }

  const visualize = (view: RenderView, viewLabel: string) => {
    if (busy.current) return
    void (async () => {
      busy.current = true
      setTurns((prev) => [
        ...prev,
        { kind: 'user', id: nextId(), text: `Show my room — ${viewLabel.toLowerCase()} view` },
      ])
      setSending(true)
      setActivity('rendering')
      const result = await postVisualize(config.apiBase, { ...base, view })
      if (result.ok) commit(result.data)
      else fail(result)
      setSending(false)
      setActivity(null)
      busy.current = false
    })()
  }

  const newChat = () => {
    if (busy.current) return
    photoUrls.current.forEach((url) => URL.revokeObjectURL(url))
    photoUrls.current = []
    clearThread(config.storeId)
    setSessionId(newSessionId())
    setTurns([])
    setPicks([])
    setSwap(null)
    setComparing([])
    setNotice(null)
  }

  return {
    sessionId,
    turns,
    sending,
    activity,
    picks,
    picking,
    notice,
    swap,
    comparing,
    send,
    retry,
    choose,
    submitBrief,
    toggleBasket,
    toggleCompare,
    removePick,
    goesWith,
    compare,
    showMore,
    exclude,
    startSwap,
    chooseAlternative,
    cancelSwap: () => setSwap(null),
    uploadPhoto,
    pickObject,
    visualize,
    newChat,
    flash: setNotice,
    dismissNotice: () => setNotice(null),
  }
}
