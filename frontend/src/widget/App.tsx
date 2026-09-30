import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getCatalogFacets } from '../api/client'
import type { CatalogFacets } from '../api/types'
import { createBridge } from './bridge'
import { Avatar } from './components/Avatar'
import { Composer } from './components/Composer'
import { ToolsSheet } from './components/ToolsSheet'
import type { WidgetConfig } from './config'
import { WidgetContext, useWidget as useWidgetContext } from './context'
import type { WidgetContextValue } from './context'
import { Icon } from './icons'
import type { IconName } from './icons'
import { VIEW_TITLES, resolveTool } from './tools'
import type { ViewName } from './tools'
import { useAgent } from './useAgent'
import { Basket } from './views/Basket'
import { BudgetShop } from './views/BudgetShop'
import { Catalog } from './views/Catalog'
import { Chat } from './views/Chat'
import { Home } from './views/Home'
import { RoomPlanner } from './views/RoomPlanner'
import { Advice, PhotoSearch, RoomContext, Visualize } from './views/ToolScreens'

/** Wide enough for the sidebar layout, whatever the host says. */
const WIDE = 820
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
const WITH_COMPOSER: ViewName[] = ['home', 'chat', 'advice']

export function App({ config }: { config: WidgetConfig }) {
  const agent = useAgent(config)
  const bridge = useMemo(() => createBridge(config.hostOrigin), [config.hostOrigin])
  const [view, setView] = useState<ViewName>(agent.turns.length ? 'chat' : 'home')
  const [facets, setFacets] = useState<CatalogFacets | null>(null)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [host, setHost] = useState({ open: true, expanded: false, mobile: false })
  const [wide, setWide] = useState(() => window.innerWidth >= WIDE)
  const fileInput = useRef<HTMLInputElement>(null)
  const unread = useRef(0)
  const expanded = wide

  // ── wiring ────────────────────────────────────────────────────────────────

  useEffect(() => {
    document.documentElement.style.setProperty('--accent', config.accent)
    document.title = `${config.assistantName} · ${config.storeName}`
  }, [config])

  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= WIDE)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  useEffect(() => {
    void getCatalogFacets(config.apiBase, config.storeId).then((result) => {
      if (result.ok) setFacets(result.data)
    })
  }, [config.apiBase, config.storeId])

  const go = useCallback((next: ViewName) => {
    setToolsOpen(false)
    setView(next)
  }, [])

  const send = agent.send
  const ask = useCallback(
    (message: string) => {
      setToolsOpen(false)
      setView('chat')
      void send(message)
    },
    [send],
  )

  const pickPhoto = useCallback(() => fileInput.current?.click(), [])

  useEffect(
    () =>
      bridge.listen((command) => {
        if (command.type === 'state') {
          setHost(command.data)
          if (command.data.open) unread.current = 0
        } else if (command.type === 'tool') {
          const target = resolveTool(command.data.tool)
          if (target) go(target)
        } else if (command.type === 'ask') {
          ask(command.data.message)
        }
      }),
    [bridge, go, ask],
  )

  useEffect(() => bridge.post({ type: 'ready' }), [bridge])

  // A reply that lands while the panel is closed puts a dot on the launcher.
  const replies = agent.turns.filter((t) => t.kind === 'assistant').length
  const seenReplies = useRef(replies)
  useEffect(() => {
    if (replies > seenReplies.current && !host.open) {
      unread.current += replies - seenReplies.current
      bridge.post({ type: 'badge', data: { count: unread.current } })
    }
    seenReplies.current = replies
  }, [replies, host.open, bridge])

  // The merchant's own analytics can follow the basket.
  const basketCount = agent.picks.length
  const firstBasket = useRef(true)
  useEffect(() => {
    if (firstBasket.current) {
      firstBasket.current = false
      return
    }
    bridge.post({ type: 'event', data: { name: 'basket:updated', payload: { count: basketCount } } })
  }, [basketCount, bridge])

  // Toasts clear themselves.
  const { notice, dismissNotice } = agent
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(dismissNotice, 2600)
    return () => window.clearTimeout(timer)
  }, [notice, dismissNotice])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !toolsOpen) bridge.post({ type: 'close' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [bridge, toolsOpen])

  const context: WidgetContextValue = {
    config,
    agent,
    bridge,
    facets,
    expanded,
    view,
    go,
    ask,
    pickPhoto,
  }

  const onFile = (file: File | undefined) => {
    if (!file) return
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      agent.flash('Please choose a JPEG, PNG or WebP photo')
      return
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      agent.flash('That photo is over 10 MB — please choose a smaller one')
      return
    }
    setView('chat')
    void agent.uploadPhoto(file)
  }

  // ── layout ────────────────────────────────────────────────────────────────

  const header = (
    <Header
      view={view}
      onBack={() => go(view === 'chat' || view === 'home' ? 'home' : agent.turns.length ? 'chat' : 'home')}
      host={host}
      expanded={expanded}
    />
  )

  const main = (
    <div className="main">
      {header}
      <div className="scroll">
        <ViewBody view={view} />
      </div>
      {WITH_COMPOSER.includes(view) && (
        <div className="composer-wrap">
          <Composer onTools={() => setToolsOpen(true)} />
          <div className="foot">
            <span>
              Powered by <b>ZORY</b>
            </span>
            <span>Prices and stock from {config.storeName}</span>
          </div>
        </div>
      )}
      {toolsOpen && <ToolsSheet onClose={() => setToolsOpen(false)} />}
      {agent.notice && (
        <div className="toast" role="status">
          <Icon name="check" size={14} strokeWidth={2.2} />
          {agent.notice}
        </div>
      )}
    </div>
  )

  return (
    <WidgetContext.Provider value={context}>
      <div className={`shell${expanded ? ' xp' : ''}`}>
        {expanded && <Sidebar />}
        {main}
      </div>
      <input
        ref={fileInput}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        hidden
        onChange={(event) => {
          onFile(event.target.files?.[0])
          event.target.value = ''
        }}
      />
    </WidgetContext.Provider>
  )
}

function ViewBody({ view }: { view: ViewName }) {
  switch (view) {
    case 'home':
      return <Home />
    case 'chat':
      return <Chat />
    case 'room-planner':
      return <RoomPlanner />
    case 'budget':
      return <BudgetShop />
    case 'catalog':
      return <Catalog />
    case 'photo':
      return <PhotoSearch />
    case 'visualize':
      return <Visualize />
    case 'compare':
      return <Basket mode="compare" />
    case 'advice':
      return <Advice />
    case 'room-context':
      return <RoomContext />
    case 'basket':
      return <Basket />
  }
}

// ── header ──────────────────────────────────────────────────────────────────

function Header({
  view,
  onBack,
  host,
  expanded,
}: {
  view: ViewName
  onBack: () => void
  host: { open: boolean; expanded: boolean; mobile: boolean }
  expanded: boolean
}) {
  const { config, agent, bridge, go } = useWidgetContext()
  const title = VIEW_TITLES[view] || config.assistantName
  const subtitle = agent.sending && view === 'chat' ? 'Thinking…' : `Your ${config.storeName} assistant`
  const showBack = view !== 'home'
  const canExpand = !host.mobile

  return (
    <header className="hdr">
      {showBack ? (
        <button className="icon-btn" onClick={onBack} aria-label="Back">
          <Icon name="back" size={18} />
        </button>
      ) : (
        !expanded && <Avatar name={config.assistantName} url={config.avatarUrl} size={36} />
      )}
      <div className="hdr-title">
        <strong>{title}</strong>
        <span>{subtitle}</span>
      </div>
      <div className="hdr-actions">
        {!expanded && (
          <button
            className="icon-btn"
            onClick={() => {
              agent.newChat()
              go('home')
            }}
            disabled={agent.sending}
            aria-label="New chat"
            title="New chat"
          >
            <Icon name="newChat" size={17} />
          </button>
        )}
        <button
          className="icon-btn"
          onClick={() => go('basket')}
          aria-pressed={view === 'basket'}
          aria-label={`Basket, ${agent.picks.length} ${agent.picks.length === 1 ? 'item' : 'items'}`}
          title="Basket"
        >
          <Icon name="bag" size={18} />
          {agent.picks.length > 0 && <span className="badge">{agent.picks.length}</span>}
        </button>
        {canExpand && (
          <button
            className="icon-btn"
            onClick={() => bridge.post({ type: host.expanded ? 'collapse' : 'expand' })}
            aria-label={host.expanded ? 'Exit full screen' : 'Expand to full screen'}
            title={host.expanded ? 'Exit full screen' : 'Full screen'}
          >
            <Icon name={host.expanded ? 'collapse' : 'expand'} size={16} />
          </button>
        )}
        <button className="icon-btn" onClick={() => bridge.post({ type: 'close' })} aria-label="Minimize chat" title="Close">
          <Icon name="close" size={18} />
        </button>
      </div>
    </header>
  )
}

// ── expanded sidebar ────────────────────────────────────────────────────────

const NAV: { view: ViewName; label: string; icon: IconName }[] = [
  { view: 'home', label: 'Assistant', icon: 'bulb' },
  { view: 'catalog', label: 'Furniture', icon: 'search' },
  { view: 'room-planner', label: 'Room planner', icon: 'sofa' },
  { view: 'compare', label: 'Comparison', icon: 'columns' },
  { view: 'basket', label: 'Basket', icon: 'bag' },
]

function Sidebar() {
  const { config, agent, view, go } = useWidgetContext()
  const firstQuestion = agent.turns.find((t) => t.kind === 'user')
  return (
    <aside className="side" aria-label="Assistant navigation">
      <div className="brand">
        <strong>ZORY</strong>
        <small>AI ASSISTANT</small>
      </div>
      <button
        className="btn"
        onClick={() => {
          agent.newChat()
          go('home')
        }}
        disabled={agent.sending}
      >
        <Icon name="newChat" size={16} /> New chat
      </button>
      {NAV.map((item) => (
        <button
          key={item.view}
          className="nav"
          aria-current={view === item.view ? 'page' : undefined}
          onClick={() => go(item.view)}
        >
          <Icon name={item.icon} size={18} />
          {item.label}
          {item.view === 'basket' && agent.picks.length > 0 && <span className="count">{agent.picks.length}</span>}
        </button>
      ))}
      {firstQuestion && firstQuestion.kind === 'user' && (
        <>
          <div className="side-label">Current conversation</div>
          <button className="nav convo" aria-current={view === 'chat' ? 'page' : undefined} onClick={() => go('chat')}>
            <Icon name="bulb" size={16} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{firstQuestion.text}</span>
          </button>
        </>
      )}
      <div className="store">
        <i>{config.storeName.charAt(0).toUpperCase()}</i>
        <div>
          <strong>{config.storeName}</strong>
          <small>Furniture shopping</small>
        </div>
      </div>
    </aside>
  )
}
