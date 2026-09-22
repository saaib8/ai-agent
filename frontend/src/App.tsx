import { useCallback, useState } from 'react'
import { ChatPanel } from './components/ChatPanel'
import { TopNav } from './components/TopNav'
import { useChat } from './hooks/useChat'
import { useConfig } from './hooks/useConfig'
import { useHealth } from './hooks/useHealth'

export default function App() {
  const config = useConfig()
  const chat = useChat()
  const { health, check } = useHealth(config.config.apiBase)
  const [draft, setDraft] = useState('')

  const handleSend = useCallback(
    (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || chat.sending) return
      void chat.send(trimmed, config.config)
      setDraft('')
    },
    [chat, config.config],
  )

  const handleNewSession = useCallback(() => {
    config.rotateSession()
    chat.reset()
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
        />
      </main>
    </div>
  )
}
