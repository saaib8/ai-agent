import { useCallback, useState } from 'react'
import { ChatPanel } from './components/ChatPanel'
import { TopNav } from './components/TopNav'
import { useChat } from './hooks/useChat'
import { useConfig } from './hooks/useConfig'
import { useHealth } from './hooks/useHealth'

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
    (ordinal: number) => {
      if (chat.sending) return
      setSwap(null)
      void chat.send('Not this one — show me something else', config.config, {
        search: { kind: 'exclude', ordinal },
      })
    },
    [chat, config.config],
  )

  const handleNewSession = useCallback(() => {
    config.rotateSession()
    chat.reset()
    setSwap(null)
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
          storeId={config.config.storeId}
          draft={draft}
          onDraftChange={setDraft}
          onSend={handleSend}
          swapRole={swap?.role ?? null}
          onSwapStart={handleSwapStart}
          onPickAlternative={handlePickAlternative}
          onShowMoreOptions={handleShowMoreOptions}
          onExcludeProduct={handleExcludeProduct}
        />
      </main>
    </div>
  )
}
