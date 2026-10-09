import { useCallback, useEffect, useRef, useState } from 'react'
import { RENDER_VIEWS } from './api/types'
import type {
  BriefAnswerAction,
  BundleAction,
  CatalogItem,
  CatalogSelection,
  FinderObject,
  GroundedProduct,
  PickView,
  ProductAction,
  RenderRoomSpec,
  RenderView,
} from './api/types'
import { getCompareGroups, postComparison } from './api/client'
import { CatalogDialog } from './components/catalog/CatalogDialog'
import { ComparisonDialog } from './components/ComparisonDialog'
import type { ComparisonPopup } from './components/ComparisonDialog'
import type { CatalogStep } from './components/catalog/CatalogDialog'
import { ChatPanel } from './components/ChatPanel'
import { TopNav } from './components/TopNav'
import { conversationLanguage, turnWords } from './lib/turnWords'
import { useChat } from './hooks/useChat'
import { useConfig } from './hooks/useConfig'
import { useHealth } from './hooks/useHealth'
import { DEFAULT_ROOM, styleLabel } from './lib/catalog'
import { DEFAULT_COMPARE_MAX, compareFamily } from './lib/compare'
import type { CheckedCard } from './lib/compare'
import type { RoomDraft, SelectedPiece } from './lib/catalog'
import { humanise } from './lib/format'

interface SwapContext {
  bundleOrdinal: number
  role: string
}

export default function App() {
  const config = useConfig()
  const chat = useChat()
  const { health, check } = useHealth(config.config.apiBase)
  const [draft, setDraft] = useState('')
  // A swap-in-progress: the customer tapped "Swap" on a room piece and is now
  // choosing a replacement from the alternatives on screen.
  const [swap, setSwap] = useState<SwapContext | null>(null)

  // Comparing: the cards checked (any number up to the server's limit, all of
  // one family) and the pop-up.
  const [compareGroups, setCompareGroups] = useState<Record<string, string>>({})
  const [compareMax, setCompareMax] = useState(DEFAULT_COMPARE_MAX)
  const [comparing, setComparing] = useState<CheckedCard[]>([])
  const [comparePopup, setComparePopup] = useState<ComparisonPopup | null>(null)
  const compareRequest = useRef(0)
  const groupsLoaded = useRef(false)

  // Which types compare with which is the server's reviewed data. Read once -
  // and again until it arrives: loaded while the API was still starting, an
  // empty list would treat a sofa and an L-shape as different kinds.
  const loadCompareGroups = useCallback(async (): Promise<Record<string, string> | null> => {
    const result = await getCompareGroups(config.config.apiBase)
    if (!result.ok) return null
    groupsLoaded.current = true
    setCompareGroups(result.data.groups)
    setCompareMax(result.data.max_products)
    return result.data.groups
  }, [config.config.apiBase])

  useEffect(() => {
    groupsLoaded.current = false
    void loadCompareGroups()
  }, [loadCompareGroups])

  useEffect(() => {
    if ((health.status === 'ok' || health.status === 'degraded') && !groupsLoaded.current) {
      void loadCompareGroups()
    }
  }, [health.status, loadCompareGroups])

  // Browse Catalogue: the picks and the room outlive the dialog, so closing
  // it to ask something in chat loses nothing.
  const [catalogStep, setCatalogStep] = useState<CatalogStep | null>(null)
  const [selection, setSelection] = useState<SelectedPiece[]>([])
  const [roomDraft, setRoomDraft] = useState<RoomDraft>(DEFAULT_ROOM)
  // The customer's room photo the server keeps for this session (emptied),
  // and the picture of it to show. One per session; a new upload replaces it.
  const [roomPhoto, setRoomPhoto] = useState<{ id: string; previewUrl: string } | null>(null)
  // Every product ever picked, so a render's pieces can be put back in the
  // selection when the customer asks to edit it.
  const picked = useRef(new Map<number, CatalogItem>())
  const storeId = config.config.storeId

  const changeSelection = useCallback((next: SelectedPiece[]) => {
    next.forEach((p) => picked.current.set(p.item.product_id, p.item))
    setSelection(next)
  }, [])

  // Products belong to one store: another store's picks mean nothing here.
  useEffect(() => {
    setSelection([])
    picked.current.clear()
  }, [storeId])

  // Cheaper options for an over-budget swap arrive as a search, but selecting one
  // must swap that room piece rather than pick a fresh product (which would
  // cross-sell a piece the room already holds). The backend marks the list with
  // the piece it is for; re-enter swap mode so an alternative's "Select" swaps.
  const lastTurn = chat.turns[chat.turns.length - 1]
  // What a tapped button says on the customer's side, in the conversation's language.
  const words = turnWords(conversationLanguage(chat.turns))
  const swapCtx =
    lastTurn?.kind === 'assistant' ? (lastTurn.data.presentation?.swap_context ?? null) : null
  useEffect(() => {
    if (swapCtx) setSwap({ bundleOrdinal: swapCtx.bundle_ordinal, role: swapCtx.role })
  }, [lastTurn?.id, swapCtx?.bundle_ordinal, swapCtx?.role])

  const handleSend = useCallback(
    (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || chat.sending) return
      setSwap(null) // a freely typed message ends any swap in progress
      void chat.send(trimmed, config.config)
      setDraft('')
    },
    [chat, config.config],
  )

  const handlePhoto = useCallback(
    (file: File) => {
      if (chat.sending) return
      setSwap(null) // a new photo moves the conversation on, like a message
      void chat.uploadPhoto(file, config.config)
    },
    [chat, config.config],
  )

  const handlePickObject = useCallback(
    (photoTurnId: string, imageId: string, object: FinderObject) => {
      if (chat.sending) return
      setSwap(null) // a pick replaces the products on screen
      void chat.pickObject(photoTurnId, imageId, object, config.config)
    },
    [chat, config.config],
  )

  const handleSwapStart = useCallback(
    (bundleOrdinal: number, role: string) => {
      if (chat.sending) return
      setSwap({ bundleOrdinal, role })
      // Deterministic: the backend runs this role's own search — never a
      // language model deciding whether "other beds" means a search or a room.
      void chat.send(words.otherRoomOptions(role), config.config, {
        bundle: { kind: 'list_alternatives', bundle_ordinal: bundleOrdinal },
      })
    },
    [chat, config.config, words],
  )

  const handlePickAlternative = useCallback(
    (alternativeOrdinal: number) => {
      if (chat.sending || swap === null) return
      void chat.send(words.useRoomOption(alternativeOrdinal, swap.role), config.config, {
        bundle: {
          kind: 'swap',
          bundle_ordinal: swap.bundleOrdinal,
          alternative_ordinal: alternativeOrdinal,
        },
      })
      setSwap(null)
    },
    [chat, config.config, swap, words],
  )

  const handleShowMoreOptions = useCallback(() => {
    if (chat.sending) return
    setSwap(null)
    // Deterministic: re-run the search in progress, excluding everything just
    // shown. No model decides whether "show me more" means a new search.
    void chat.send(words.differentOptions, config.config, {
      search: { kind: 'more_options' },
    })
  }, [chat, config.config, words])

  const handleExcludeProduct = useCallback(
    (product: GroundedProduct) => {
      if (chat.sending || product.presented_ordinal == null) return
      setSwap(null)
      // The message names the piece so the conversation record reads clearly;
      // `rejected` shows it on the user's turn so the thread makes visible which
      // one was passed on, not just that something was.
      void chat.send(words.notThisOne(product.name_english), config.config, {
        search: { kind: 'exclude', ordinal: product.presented_ordinal },
        rejected: {
          name: product.name_english,
          imageUrl: product.image_url,
          label: words.notThisOneLabel,
        },
      })
    },
    [chat, config.config, words],
  )

  // ── picks: ticking, what goes with one, comparing two ───────────────────────

  const handleTogglePick = useCallback(
    async (product: GroundedProduct, listRevision: number) => {
      const ordinal = product.presented_ordinal
      if (chat.sending || chat.picking || ordinal == null) return
      const already = chat.picks.find((p) =>
        (p.positions ?? []).some(
          (position) => position.list_revision === listRevision && position.ordinal === ordinal,
        ),
      )
      if (already) {
        void chat.changePicks({ kind: 'deselect', pick: already.pick }, config.config)
        return
      }
      const saved = await chat.changePicks(
        { kind: 'select', ordinal, list_revision: listRevision },
        config.config,
      )
      // The first pick of its kind: show what goes with it, as a turn of its
      // own. A second of the same kind - picked to compare - stays silent.
      const chosen = saved?.picks.find((p) => p.pick === saved.goes_with)
      if (chosen) {
        setSwap(null)
        void chat.send(words.likeProduct(chosen.name_english), config.config, {
          product: { kind: 'goes_with', pick: chosen.pick },
        })
      }
    },
    [chat, config.config, words],
  )

  const handleRemovePick = useCallback(
    (pick: PickView) => {
      if (chat.sending || chat.picking) return
      void chat.changePicks({ kind: 'deselect', pick: pick.pick }, config.config)
    },
    [chat, config.config],
  )

  const handleGoesWith = useCallback(
    (pick: PickView) => {
      if (chat.sending || chat.picking) return
      setSwap(null)
      void chat.send(words.whatGoesWith(pick.name_english), config.config, {
        product: { kind: 'goes_with', pick: pick.pick },
      })
    },
    [chat, config.config, words],
  )

  // ── comparing the checked cards, in a pop-up ─────────────────────────────

  const familyOf = useCallback(
    (product: GroundedProduct) => compareFamily(product, compareGroups),
    [compareGroups],
  )

  const handleToggleCompare = useCallback(
    async (product: GroundedProduct, listRevision: number) => {
      const ordinal = product.presented_ordinal
      const groups = groupsLoaded.current ? compareGroups : await loadCompareGroups()
      const family = compareFamily(product, groups ?? compareGroups)
      if (ordinal == null || family == null) return
      const already = comparing.find(
        (c) => c.listRevision === listRevision && c.ordinal === ordinal,
      )
      if (already) {
        setComparing(comparing.filter((c) => c !== already))
        return
      }
      if (comparing.length >= compareMax || (comparing[0] && comparing[0].family !== family)) {
        return
      }
      // Checking only marks it: the comparison waits for the Compare button.
      setComparing([
        ...comparing,
        {
          listRevision,
          ordinal,
          family,
          name: product.name_english,
          imageUrl: product.image_url || null,
        },
      ])
    },
    [comparing, compareGroups, compareMax, loadCompareGroups],
  )

  const handleCompare = useCallback(async () => {
    if (comparing.length < 2) return
    const names = comparing.map((card) => card.name)
    const request = ++compareRequest.current
    setComparePopup({ status: 'loading', names })
    const result = await postComparison(config.config.apiBase, {
      session_id: config.config.sessionId,
      store_id: config.config.storeId,
      cards: comparing.map((card) => ({ list_revision: card.listRevision, ordinal: card.ordinal })),
    })
    if (request !== compareRequest.current) return // closed while loading
    setComparePopup(
      result.ok
        ? { status: 'ready', names, data: result.data }
        : { status: 'error', names, message: result.error.message },
    )
  }, [comparing, config.config])

  // Closing the pop-up keeps the cards checked: one can be swapped for another
  // and compared again. Clearing is its own action.
  const handleCloseCompare = useCallback(() => {
    compareRequest.current += 1
    setComparePopup(null)
  }, [])

  const handleUncheckCompare = useCallback((card: CheckedCard) => {
    setComparing((checked) => checked.filter((c) => c !== card))
  }, [])

  const handleClearCompare = useCallback(() => setComparing([]), [])

  const handleBriefSubmit = useCallback(
    (answer: BriefAnswerAction, summary: string) => {
      if (chat.sending || chat.picking) return
      setSwap(null)
      void chat.send(summary, config.config, { search: answer })
    },
    [chat, config.config],
  )

  const handleChoice = useCallback(
    (value: string, action?: ProductAction | null, bundle?: BundleAction | null) => {
      if (bundle) {
        // The yes/no on an over-budget swap: a structured room edit that answers
        // the held offer deterministically, never routed through the model.
        if (chat.sending || chat.picking) return
        setSwap(null)
        void chat.send(value, config.config, { bundle })
        return
      }
      if (!action) {
        handleSend(value)
        return
      }
      if (chat.sending || chat.picking) return
      setSwap(null)
      void chat.send(value, config.config, { product: action })
    },
    [chat, config.config, handleSend],
  )

  const handleVisualize = useCallback(
    (view: RenderView, viewLabel: string) => {
      if (chat.sending) return
      setSwap(null)
      void chat.visualize(view, viewLabel, config.config)
    },
    [chat, config.config],
  )

  /** The session's room photo id - after uploading a new photo first, when one
   *  is given. Null when there is none, or the upload was refused (shown in the
   *  chat). */
  const roomPhotoFor = useCallback(
    async (file: File | null): Promise<string | null> => {
      if (!file) return roomPhoto?.id ?? null
      const uploaded = await chat.uploadRoomPhoto(file, config.config)
      if (!uploaded) return null
      setRoomPhoto({ id: uploaded.data.room_photo_id, previewUrl: uploaded.previewUrl })
      return uploaded.data.room_photo_id
    },
    [chat, config.config, roomPhoto],
  )

  /** A render in their room failed because the session no longer holds the
   *  photo (it expired): forget it, so the next try asks for a new one. */
  const forgetLostRoomPhoto = useCallback((code: string | null, photoId: string) => {
    if (code !== 'room_photo_not_found') return
    // Only the photo that was asked for: an older render's photo going
    // missing says nothing about the one uploaded since.
    setRoomPhoto((current) => (current?.id === photoId ? null : current))
  }, [])

  const handleVisualizeInRoom = useCallback(
    async (file: File | null) => {
      if (chat.sending) return
      setSwap(null)
      const photoId = await roomPhotoFor(file)
      if (!photoId) return
      const code = await chat.visualize('corner', 'Your room', config.config, photoId)
      forgetLostRoomPhoto(code, photoId)
    },
    [chat, config.config, roomPhotoFor, forgetLostRoomPhoto],
  )

  const handleRerenderInRoom = useCallback(
    async (photoId: string) => {
      if (chat.sending) return
      setSwap(null)
      const code = await chat.visualize('corner', 'Your room', config.config, photoId)
      forgetLostRoomPhoto(code, photoId)
    },
    [chat, config.config, forgetLostRoomPhoto],
  )

  const closeCatalog = useCallback(() => setCatalogStep(null), [])

  const renderSelection = useCallback(
    async (selection: CatalogSelection, view: RenderView, summary: string) => {
      if (chat.sending) return
      setSwap(null)
      const code = await chat.visualizeSelection(selection, view, summary, config.config)
      if (selection.room_photo_id) forgetLostRoomPhoto(code, selection.room_photo_id)
    },
    [chat, config.config, forgetLostRoomPhoto],
  )

  const handleCatalogVisualize = useCallback(
    (
      items: CatalogSelection['items'],
      room: RenderRoomSpec | null,
      view: RenderView,
      photo: File | 'kept' | null,
    ) => {
      const units = items.reduce((n, item) => n + item.quantity, 0)
      const pieces = `${units} ${units === 1 ? 'piece' : 'pieces'}`
      setCatalogStep(null)
      if (photo !== null || room === null) {
        void (async () => {
          const photoId = await roomPhotoFor(photo instanceof File ? photo : null)
          if (!photoId) return
          // The new photo is now the session's; the draft no longer holds it.
          setRoomDraft((draft) => ({ ...draft, photoFile: null }))
          renderSelection(
            { items, room: null, room_photo_id: photoId },
            view,
            `Place my selection in my room — ${pieces}`,
          )
        })()
        return
      }
      const viewLabel = RENDER_VIEWS.find((v) => v.value === view)?.label ?? view
      renderSelection(
        { items, room },
        view,
        `Visualize my selection — ${pieces} in a ` +
          `${room.length_m} × ${room.width_m} m ${styleLabel(room.style).toLowerCase()} ` +
          `${humanise(room.room_type)}, ${viewLabel.toLowerCase()} view`,
      )
    },
    [renderSelection, roomPhotoFor],
  )

  const handleRerenderSelection = useCallback(
    (selection: CatalogSelection, view: RenderView, viewLabel: string) =>
      renderSelection(
        selection,
        view,
        selection.room_photo_id
          ? 'Show my selection in my room again'
          : `Show my selection — ${viewLabel.toLowerCase()} view`,
      ),
    [renderSelection],
  )

  const handleEditSelection = useCallback((selection: CatalogSelection, view: RenderView) => {
    const pieces = selection.items.flatMap((item) => {
      const known = picked.current.get(item.product_id)
      return known ? [{ item: known, quantity: item.quantity }] : []
    })
    if (pieces.length) setSelection(pieces)
    const room = selection.room
    setRoomDraft((draft) =>
      room
        ? {
            ...draft,
            roomType: room.room_type,
            style: room.style,
            length: String(room.length_m),
            width: String(room.width_m),
            view,
            usePhoto: false,
          }
        : // Drawn in their own room: reopen it there, with the photo kept.
          { ...draft, usePhoto: true, photoFile: null },
    )
    setCatalogStep('browse')
  }, [])

  const handleNewSession = useCallback(() => {
    config.rotateSession()
    chat.reset()
    setSwap(null)
    setSelection([])
    setRoomPhoto(null)
    setRoomDraft((draft) => ({ ...draft, photoFile: null }))
    handleCloseCompare()
    handleClearCompare()
  }, [config, chat, handleCloseCompare, handleClearCompare])

  return (
    <div className="flex h-screen flex-col overflow-hidden text-ink">
      <TopNav
        config={config}
        health={health}
        onCheckHealth={check}
        onNewSession={handleNewSession}
        revision={chat.revision}
      />
      <main className="min-h-0 flex-1">
        <ChatPanel
          turns={chat.turns}
          sending={chat.sending}
          activity={chat.activity}
          storeId={config.config.storeId}
          draft={draft}
          onDraftChange={setDraft}
          onSend={handleSend}
          onPhoto={handlePhoto}
          onPickObject={handlePickObject}
          swapRole={swap?.role ?? null}
          onSwapStart={handleSwapStart}
          onPickAlternative={handlePickAlternative}
          onShowMoreOptions={handleShowMoreOptions}
          onExcludeProduct={handleExcludeProduct}
          onVisualize={handleVisualize}
          onVisualizeInRoom={(file) => void handleVisualizeInRoom(file)}
          onRerenderInRoom={(photoId) => void handleRerenderInRoom(photoId)}
          roomPhotoPreview={roomPhoto?.previewUrl ?? null}
          onOpenCatalog={() => setCatalogStep('browse')}
          onRerenderSelection={handleRerenderSelection}
          onEditSelection={handleEditSelection}
          picks={chat.picks}
          picking={chat.picking}
          picksError={chat.picksError}
          onTogglePick={handleTogglePick}
          onRemovePick={handleRemovePick}
          onGoesWith={handleGoesWith}
          comparing={comparing}
          compareMax={compareMax}
          familyOf={familyOf}
          onToggleCompare={handleToggleCompare}
          onCompare={handleCompare}
          onUncheckCompare={handleUncheckCompare}
          onClearCompare={handleClearCompare}
          onChoice={handleChoice}
          onBriefSubmit={handleBriefSubmit}
        />
      </main>
      {comparePopup && <ComparisonDialog popup={comparePopup} onClose={handleCloseCompare} />}
      {catalogStep && (
        <CatalogDialog
          apiBase={config.config.apiBase}
          storeId={storeId}
          selection={selection}
          onSelectionChange={changeSelection}
          room={roomDraft}
          onRoomChange={setRoomDraft}
          initialStep={catalogStep}
          busy={chat.sending}
          onClose={closeCatalog}
          onVisualize={handleCatalogVisualize}
          roomPhotoPreview={roomPhoto?.previewUrl ?? null}
        />
      )}
    </div>
  )
}
