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
import { CatalogDialog } from './components/catalog/CatalogDialog'
import type { CatalogStep } from './components/catalog/CatalogDialog'
import { ChatPanel } from './components/ChatPanel'
import { TopNav } from './components/TopNav'
import { useChat } from './hooks/useChat'
import { useConfig } from './hooks/useConfig'
import { useHealth } from './hooks/useHealth'
import { DEFAULT_ROOM, styleLabel } from './lib/catalog'
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

  // Browse Catalogue: the picks and the room outlive the dialog, so closing
  // it to ask something in chat loses nothing.
  const [catalogStep, setCatalogStep] = useState<CatalogStep | null>(null)
  const [selection, setSelection] = useState<SelectedPiece[]>([])
  const [roomDraft, setRoomDraft] = useState<RoomDraft>(DEFAULT_ROOM)
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
      void chat.send(`Show me other ${role} options`, config.config, {
        bundle: { kind: 'list_alternatives', bundle_ordinal: bundleOrdinal },
      })
    },
    [chat, config.config],
  )

  const handlePickAlternative = useCallback(
    (alternativeOrdinal: number) => {
      if (chat.sending || swap === null) return
      void chat.send(`Use option ${alternativeOrdinal} for the ${swap.role}`, config.config, {
        bundle: {
          kind: 'swap',
          bundle_ordinal: swap.bundleOrdinal,
          alternative_ordinal: alternativeOrdinal,
        },
      })
      setSwap(null)
    },
    [chat, config.config, swap],
  )

  const handleShowMoreOptions = useCallback(() => {
    if (chat.sending) return
    setSwap(null)
    // Deterministic: re-run the search in progress, excluding everything just
    // shown. No model decides whether "show me more" means a new search.
    void chat.send('Show me different options', config.config, {
      search: { kind: 'more_options' },
    })
  }, [chat, config.config])

  const handleExcludeProduct = useCallback(
    (product: GroundedProduct) => {
      if (chat.sending || product.presented_ordinal == null) return
      setSwap(null)
      // The message names the piece so the conversation record reads clearly;
      // `rejected` shows it on the user's turn so the thread makes visible which
      // one was passed on, not just that something was.
      void chat.send(`Not this one — the ${product.name_english}`, config.config, {
        search: { kind: 'exclude', ordinal: product.presented_ordinal },
        rejected: { name: product.name_english, imageUrl: product.image_url },
      })
    },
    [chat, config.config],
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
        void chat.send(`I like the ${chosen.name_english}`, config.config, {
          product: { kind: 'goes_with', pick: chosen.pick },
        })
      }
    },
    [chat, config.config],
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
      void chat.send(`What goes with the ${pick.name_english}?`, config.config, {
        product: { kind: 'goes_with', pick: pick.pick },
      })
    },
    [chat, config.config],
  )

  const handleComparePicks = useCallback(
    (first: PickView, second: PickView) => {
      if (chat.sending || chat.picking) return
      setSwap(null)
      void chat.send(
        `Compare the ${first.name_english} and the ${second.name_english}`,
        config.config,
        { product: { kind: 'compare', picks: [first.pick, second.pick] } },
      )
    },
    [chat, config.config],
  )

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

  const closeCatalog = useCallback(() => setCatalogStep(null), [])

  const renderSelection = useCallback(
    (selection: CatalogSelection, view: RenderView, summary: string) => {
      if (chat.sending) return
      setSwap(null)
      void chat.visualizeSelection(selection, view, summary, config.config)
    },
    [chat, config.config],
  )

  const handleCatalogVisualize = useCallback(
    (items: CatalogSelection['items'], room: RenderRoomSpec, view: RenderView) => {
      const units = items.reduce((n, item) => n + item.quantity, 0)
      const viewLabel = RENDER_VIEWS.find((v) => v.value === view)?.label ?? view
      setCatalogStep(null)
      renderSelection(
        { items, room },
        view,
        `Visualize my selection — ${units} ${units === 1 ? 'piece' : 'pieces'} in a ` +
          `${room.length_m} × ${room.width_m} m ${styleLabel(room.style).toLowerCase()} ` +
          `${humanise(room.room_type)}, ${viewLabel.toLowerCase()} view`,
      )
    },
    [renderSelection],
  )

  const handleRerenderSelection = useCallback(
    (selection: CatalogSelection, view: RenderView, viewLabel: string) =>
      renderSelection(selection, view, `Show my selection — ${viewLabel.toLowerCase()} view`),
    [renderSelection],
  )

  const handleEditSelection = useCallback((selection: CatalogSelection, view: RenderView) => {
    const pieces = selection.items.flatMap((item) => {
      const known = picked.current.get(item.product_id)
      return known ? [{ item: known, quantity: item.quantity }] : []
    })
    if (pieces.length) setSelection(pieces)
    setRoomDraft({
      roomType: selection.room.room_type,
      style: selection.room.style,
      length: String(selection.room.length_m),
      width: String(selection.room.width_m),
      view,
    })
    setCatalogStep('browse')
  }, [])

  const handleNewSession = useCallback(() => {
    config.rotateSession()
    chat.reset()
    setSwap(null)
    setSelection([])
  }, [config, chat])

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
          onOpenCatalog={() => setCatalogStep('browse')}
          onRerenderSelection={handleRerenderSelection}
          onEditSelection={handleEditSelection}
          picks={chat.picks}
          picking={chat.picking}
          picksError={chat.picksError}
          onTogglePick={handleTogglePick}
          onRemovePick={handleRemovePick}
          onGoesWith={handleGoesWith}
          onComparePicks={handleComparePicks}
          onChoice={handleChoice}
          onBriefSubmit={handleBriefSubmit}
        />
      </main>
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
        />
      )}
    </div>
  )
}
