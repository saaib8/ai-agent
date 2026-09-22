import { useEffect, useState } from 'react'
import type { HealthState } from '../hooks/useHealth'
import type { UseConfig } from '../hooks/useConfig'
import { PlusIcon, SettingsIcon } from './icons'
import { SettingsPopover } from './SettingsPopover'
import { Dot, Pill } from './ui/Pill'

interface TopNavProps {
  config: UseConfig
  health: HealthState
  onCheckHealth: () => void
  onNewSession: () => void
  revision: number | null
}

type Tone = 'neutral' | 'accent' | 'success' | 'warn' | 'danger'

function healthLabel(health: HealthState): { label: string; tone: Tone } {
  switch (health.status) {
    case 'ok':
      return { label: 'Online', tone: 'success' }
    case 'degraded':
      return { label: 'Degraded', tone: 'warn' }
    case 'checking':
      return { label: 'Checking', tone: 'warn' }
    case 'unreachable':
      return { label: 'Offline', tone: 'danger' }
    default:
      return { label: 'Health', tone: 'neutral' }
  }
}

export function TopNav({ config, health, onCheckHealth, onNewSession, revision }: TopNavProps) {
  const [open, setOpen] = useState(false)
  const status = healthLabel(health)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <header className="relative z-30 border-b border-line bg-surface">
      <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-clay font-display text-base font-semibold text-white">
          Z
        </div>
        <div className="min-w-0">
          <div className="font-display text-[17px] leading-none text-ink">ZORY</div>
          <div className="hidden truncate text-xs text-muted sm:block">
            Commerce &amp; interior-design assistant
          </div>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setOpen(true)}
            className="flex h-11 items-center rounded-full transition hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 sm:h-9"
            aria-label={`Service status: ${status.label}. Open settings`}
          >
            <Pill tone={status.tone}>
              <Dot tone={status.tone} />
              <span className="hidden sm:inline">{status.label}</span>
            </Pill>
          </button>

          <button
            onClick={onNewSession}
            aria-label="New chat"
            className="inline-flex h-11 items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-sm font-medium text-ink transition hover:border-line-strong hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 sm:h-9"
          >
            <PlusIcon size={16} />
            <span className="hidden sm:inline">New chat</span>
          </button>

          <button
            onClick={() => setOpen((v) => !v)}
            className={`inline-flex h-11 w-11 items-center justify-center rounded-full border transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 sm:h-9 sm:w-9 ${
              open
                ? 'border-clay/40 bg-clay-soft/60 text-clay'
                : 'border-line bg-surface text-muted hover:border-line-strong hover:text-ink'
            }`}
            aria-label="Settings"
            aria-expanded={open}
          >
            <SettingsIcon size={18} />
          </button>
        </div>
      </div>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} aria-hidden="true" />
          <div
            role="dialog"
            aria-label="Settings"
            className="absolute right-3 top-[calc(100%+8px)] z-40 max-h-[80vh] w-[min(360px,calc(100vw-24px))] animate-fade overflow-y-auto rounded-2xl border border-line bg-surface shadow-soft"
          >
            <SettingsPopover
              config={config}
              health={health}
              onCheckHealth={onCheckHealth}
              revision={revision}
            />
          </div>
        </>
      )}
    </header>
  )
}
